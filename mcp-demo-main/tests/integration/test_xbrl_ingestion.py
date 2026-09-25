import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.ingest import ingest_company_facts
from sec_research.sec_client import Company, SecClient, SecClientError


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
FIXTURE = Path(__file__).parents[1] / "fixtures" / "sec" / "companyfacts.json"
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


@pytest.mark.asyncio
async def test_company_facts_normalize_multiunit_skip_unsafe_and_deduplicate() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    company = Company("0000320193", "AAPL", "Apple Inc.")
    suffix = uuid4().int % 800_000 + 100_000
    accession = f"0000320193-24-{suffix:06d}"
    for taxonomy in payload["facts"].values():
        for concept in taxonomy.values():
            for entries in concept["units"].values():
                for entry in entries:
                    if entry.get("accn"):
                        entry["accn"] = accession

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("CIK0000320193.json")
        return httpx.Response(200, json=payload)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        _ensure_filing(connection, company, accession)
        async with SecClient(
            "sec-research-demo contact@example.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            first = await ingest_company_facts(connection, client, company)
            second = await ingest_company_facts(connection, client, company)

    assert first.inserted == 2
    assert first.duplicates == 1
    assert first.skipped == 2
    assert "CUSTOM_TAXONOMY_UNSUPPORTED:aapl:CustomMetric" in first.warnings
    assert "MISSING_ACCESSION:us-gaap:Revenues" in first.warnings
    assert second.inserted == 0
    assert second.updated == 2

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        facts = connection.execute(
            """
            SELECT taxonomy, concept, unit, value, fiscal_year, fiscal_period,
                   form, filed_date, accession_number
            FROM xbrl_facts
            WHERE accession_number = %s
            ORDER BY unit
            """,
            (accession,),
        ).fetchall()

    assert len(facts) == 2
    assert {row[2] for row in facts} == {"USD", "shares"}
    assert all(row[0:2] == ("us-gaap", "Revenues") for row in facts)
    assert all(row[4:7] == (2024, "FY", "10-K") for row in facts)


@pytest.mark.asyncio
async def test_temporary_sec_failure_does_not_persist_partial_facts() -> None:
    assert TEST_DATABASE_URL is not None
    company = Company("0000320193", "AAPL", "Apple Inc.")

    async def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        before = connection.execute("SELECT count(*) FROM xbrl_facts").fetchone()
        async with SecClient(
            "sec-research-demo contact@example.com",
            max_retries=0,
            transport=httpx.MockTransport(unavailable),
        ) as client:
            with pytest.raises(SecClientError) as error:
                await ingest_company_facts(connection, client, company)
        after = connection.execute("SELECT count(*) FROM xbrl_facts").fetchone()

    assert error.value.retryable is True
    assert after == before


def _ensure_filing(
    connection: psycopg.Connection[tuple[object, ...]],
    company: Company,
    accession: str,
) -> None:
    connection.execute(
        """
        INSERT INTO companies (cik, ticker, company_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (cik) DO NOTHING
        """,
        (company.cik, company.ticker, company.company_name),
    )
    connection.execute(
        """
        INSERT INTO filings (
            accession_number, cik, form, filed_date, period_end,
            primary_document, source_url
        ) VALUES (
            %s, %s, '10-K', '2024-11-01', '2024-09-28',
            'aapl-20240928.htm', %s
        ) ON CONFLICT (accession_number) DO NOTHING
        """,
        (
            accession,
            company.cik,
            "https://www.sec.gov/Archives/edgar/data/320193/"
            f"{accession.replace('-', '')}/aapl-20240928.htm",
        ),
    )
