import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.ingest import (
    FilingMetadata,
    RawStore,
    ResearchScope,
    discover_filings,
    download_filings,
    start_run,
)
from sec_research.sec_client import Company, SecClient, build_filing_url


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


def test_discovery_uses_five_completed_years_and_excludes_amendments() -> None:
    submissions = {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-25-000001",
                    "0000320193-24-000001",
                    "0000320193-24-000002",
                    "0000320193-23-000001",
                    "0000320193-22-000001",
                    "0000320193-21-000001",
                    "0000320193-20-000001",
                    "0000320193-19-000001",
                    "0000320193-24-000003",
                ],
                "filingDate": [
                    "2025-08-01",
                    "2024-11-01",
                    "2024-08-01",
                    "2023-11-01",
                    "2022-11-01",
                    "2021-11-01",
                    "2020-11-01",
                    "2019-11-01",
                    "2024-11-05",
                ],
                "reportDate": [
                    "2025-06-28",
                    "2024-09-28",
                    "2024-06-29",
                    "2023-09-30",
                    "2022-09-24",
                    "2021-09-25",
                    "2020-09-26",
                    "2019-09-28",
                    "2024-09-28",
                ],
                "form": [
                    "10-Q",
                    "10-K",
                    "10-Q",
                    "10-K",
                    "10-K",
                    "10-K",
                    "10-K",
                    "10-K",
                    "10-K/A",
                ],
                "primaryDocument": [f"filing-{index}.htm" for index in range(9)],
            }
        }
    }

    filings = discover_filings(submissions, ResearchScope.create())

    assert {filing.period_end.year for filing in filings} == {
        2020,
        2021,
        2022,
        2023,
        2024,
    }
    assert {filing.form for filing in filings} == {"10-K", "10-Q"}
    assert all(not filing.form.endswith("/A") for filing in filings)


@pytest.mark.asyncio
async def test_download_slice_adds_skips_quarantines_and_preserves_failures(
    tmp_path: Path,
) -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    company = Company("0000320193", "AAPL", "Apple Inc.", "Nasdaq")
    suffix = uuid4().int % 800_000 + 100_000
    good = _filing(f"0000320193-25-{suffix:06d}", "aapl-20250628.htm")
    quarantined = _filing(f"0000320193-25-{suffix + 1:06d}", "aapl-bad.htm")
    failed = _filing(f"0000320193-25-{suffix + 2:06d}", "aapl-temporary.htm")
    good_html = (FIXTURES / "filing.html").read_bytes()
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("aapl-20250628.htm"):
            return httpx.Response(
                200,
                content=good_html,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        if request.url.path.endswith("aapl-bad.htm"):
            return httpx.Response(
                200,
                content=b"<html><body>wrong company</body></html>",
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(503)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        run_id = start_run(
            connection,
            ResearchScope.create(),
            f"filing-download-{uuid4()}",
        )
        async with SecClient(
            "sec-research-demo contact@example.com",
            max_retries=0,
            transport=httpx.MockTransport(handler),
        ) as client:
            summary = await download_filings(
                connection,
                run_id,
                client,
                RawStore(tmp_path),
                company,
                [good, quarantined, failed],
            )
            repeated = await download_filings(
                connection,
                run_id,
                client,
                RawStore(tmp_path),
                company,
                [good],
            )

    assert summary == {
        "added": 1,
        "updated": 0,
        "skipped": 0,
        "quarantined": 1,
        "failed": 1,
    }
    assert repeated == {
        "added": 0,
        "updated": 0,
        "skipped": 1,
        "quarantined": 0,
        "failed": 0,
    }
    assert len(requests) == 3

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        states = dict(
            connection.execute(
                """
                SELECT accession_number, processing_status
                FROM filings
                WHERE accession_number IN (%s, %s, %s)
                """,
                (
                    good.accession_number,
                    quarantined.accession_number,
                    failed.accession_number,
                ),
            ).fetchall()
        )
    assert states == {
        good.accession_number: "downloaded",
        quarantined.accession_number: "quarantined",
        failed.accession_number: "failed",
    }
    assert (
        tmp_path
        / company.cik
        / good.accession_number
        / "filing.html"
    ).read_bytes() == good_html


def _filing(accession: str, primary_document: str) -> FilingMetadata:
    return FilingMetadata.create(
        cik="0000320193",
        ticker="AAPL",
        company_name="Apple Inc.",
        form="10-Q",
        filed_date="2025-08-01",
        period_end="2025-06-28",
        accession_number=accession,
        primary_document=primary_document,
        source_url=build_filing_url("0000320193", accession, primary_document),
    )
