import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.ingest import (
    RawStore,
    ResearchScope,
    discover_filings,
    download_filings,
    finish_run,
    ingest_company_facts,
    start_run,
)
from sec_research.parser import parse_filing
from sec_research.rag import persist_sections_and_chunks
from sec_research.sec_client import Company, SecClient


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


@pytest.mark.asyncio
async def test_fixture_pipeline_is_idempotent_and_quarantine_is_not_searchable(
    tmp_path: Path,
) -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    suffix = uuid4().int % 700_000 + 100_000
    good_accession = f"0000320193-24-{suffix:06d}"
    bad_accession = f"0000320193-24-{suffix + 1:06d}"
    company = Company("0000320193", "AAPL", "Apple Inc.", "Nasdaq")
    submissions = _submissions(good_accession, bad_accession)
    filings = discover_filings(submissions, ResearchScope.create(years=1))
    xbrl = json.loads((FIXTURES / "companyfacts.json").read_text(encoding="utf-8"))
    for taxonomy in xbrl["facts"].values():
        for concept in taxonomy.values():
            for entries in concept["units"].values():
                for entry in entries:
                    if entry.get("accn"):
                        entry["accn"] = good_accession
    good_html = (FIXTURES / "10k.html").read_bytes().replace(
        b"<body>", b"<body><p>0000320193</p>", 1
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if "companyfacts" in request.url.path:
            return httpx.Response(200, json=xbrl)
        if request.url.path.endswith("good.htm"):
            return httpx.Response(
                200,
                content=good_html,
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(
            200,
            content=b"<html><body>identity mismatch</body></html>",
            headers={"Content-Type": "text/html"},
        )

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        run_id = start_run(
            connection,
            ResearchScope.create(years=1),
            f"checkpoint-b-{uuid4()}",
        )
        async with SecClient(
            "sec-research-demo contact@example.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            first_download = await download_filings(
                connection,
                run_id,
                client,
                RawStore(tmp_path),
                company,
                filings,
            )
            parsed = parse_filing(good_html, "10-K")
            persist_sections_and_chunks(connection, filings[0], parsed.sections)
            first_xbrl = await ingest_company_facts(connection, client, company)
            counts_before = _pipeline_counts(connection, good_accession)

            second_download = await download_filings(
                connection,
                run_id,
                client,
                RawStore(tmp_path),
                company,
                filings,
            )
            persist_sections_and_chunks(connection, filings[0], parsed.sections)
            second_xbrl = await ingest_company_facts(connection, client, company)
            counts_after = _pipeline_counts(connection, good_accession)
        run_counts = finish_run(connection, run_id, status="completed")

    assert first_download["added"] == 1
    assert first_download["quarantined"] == 1
    assert second_download["skipped"] == 1
    assert second_download["quarantined"] == 1
    assert first_xbrl.inserted == 2
    assert second_xbrl.updated == 2
    assert counts_before == counts_after == (1, 5, 5, 2)
    assert run_counts == {"downloaded": 1, "quarantined": 1}

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        quarantined_corpus = connection.execute(
            """
            SELECT count(*)
            FROM filing_sections
            WHERE accession_number = %s
            """,
            (bad_accession,),
        ).fetchone()
        failure = connection.execute(
            """
            SELECT accession_number, stage, error_code
            FROM ingestion_items
            WHERE run_id = %s AND accession_number = %s
            """,
            (run_id, bad_accession),
        ).fetchone()
    assert quarantined_corpus == (0,)
    assert failure == (bad_accession, "quarantined", "FILING_IDENTITY_INVALID")


def _submissions(good_accession: str, bad_accession: str) -> dict[str, object]:
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [good_accession, bad_accession],
                "filingDate": ["2024-11-01", "2024-11-01"],
                "reportDate": ["2024-09-28", "2024-09-28"],
                "form": ["10-K", "10-K"],
                "primaryDocument": ["good.htm", "bad.htm"],
            }
        },
    }


def _pipeline_counts(
    connection: psycopg.Connection[tuple[object, ...]],
    accession: str,
) -> tuple[int, int, int, int]:
    return connection.execute(
        """
        SELECT
            (SELECT count(*) FROM filings WHERE accession_number = %s),
            (SELECT count(*) FROM filing_sections WHERE accession_number = %s),
            (SELECT count(*) FROM chunks JOIN filing_sections USING (section_id)
             WHERE accession_number = %s),
            (SELECT count(*) FROM xbrl_facts WHERE accession_number = %s)
        """,
        (accession, accession, accession, accession),
    ).fetchone()
