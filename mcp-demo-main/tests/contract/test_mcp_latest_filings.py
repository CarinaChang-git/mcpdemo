import os
from typing import Any

import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.mcp_server import get_latest_filings_tool
from sec_research.sec_client import SecClientError


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 契約測試",
)


class FakeSecClient:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.requested_cik: str | None = None

    async def fetch_submissions(self, cik: str) -> dict[str, Any]:
        self.requested_cik = cik
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_latest_filings_supports_three_forms_limit_and_corpus_hit() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    submissions = _submissions()
    _seed_corpus_hit(submissions["filings"]["recent"]["accessionNumber"][0])
    client = FakeSecClient(submissions)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        response = await get_latest_filings_tool(
            connection,
            client,
            {"ticker": "aapl", "forms": ["10-k", "10-Q", "8-K"], "limit": 2},
        )

    assert response["status"] == "ok"
    assert client.requested_cik == "0000320193"
    assert len(response["data"]["filings"]) == 2
    assert {item["form"] for item in response["data"]["filings"]} <= {
        "10-K",
        "10-Q",
        "8-K",
    }
    assert response["data"]["filings"][0]["in_local_corpus"] is True
    assert all(
        item["source_url"].startswith("https://www.sec.gov/Archives/")
        and item["retrieved_at"].endswith("+00:00")
        for item in response["data"]["filings"]
    )


@pytest.mark.asyncio
async def test_latest_filings_maps_rate_limit_and_dependency_unavailable() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        limited = await get_latest_filings_tool(
            connection,
            FakeSecClient(
                SecClientError("SEC_RATE_LIMITED", "429", retryable=True)
            ),
            {"ticker": "AAPL", "forms": ["8-K"], "limit": 1},
        )
        unavailable = await get_latest_filings_tool(
            connection,
            FakeSecClient(
                SecClientError(
                    "SEC_DEPENDENCY_UNAVAILABLE", "網路錯誤", retryable=True
                )
            ),
            {"ticker": "AAPL", "forms": ["10-K"], "limit": 1},
        )

    assert limited["error"]["code"] == "RATE_LIMITED"
    assert unavailable["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_latest_filings_rejects_invalid_scope() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    client = FakeSecClient(_submissions())

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        invalid = [
            await get_latest_filings_tool(connection, client, payload)
            for payload in (
                {"ticker": "TSLA"},
                {"ticker": "AAPL", "forms": ["S-1"]},
                {"ticker": "AAPL", "limit": 21},
            )
        ]

    assert all(item["error"]["code"] == "INVALID_ARGUMENT" for item in invalid)


def _submissions() -> dict[str, Any]:
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-25-000079",
                    "0000320193-25-000078",
                    "0000320193-25-000077",
                ],
                "filingDate": ["2025-08-01", "2025-07-31", "2025-07-30"],
                "reportDate": ["2025-06-28", "2025-06-27", "2025-06-26"],
                "form": ["10-Q", "8-K", "10-K"],
                "primaryDocument": ["q.htm", "current.htm", "annual.htm"],
            }
        },
    }


def _seed_corpus_hit(accession: str) -> None:
    assert TEST_DATABASE_URL is not None
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        connection.execute(
            """
            INSERT INTO companies (cik, ticker, company_name)
            VALUES ('0000320193', 'AAPL', 'Apple Inc.')
            ON CONFLICT (cik) DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO filings (
                accession_number, cik, form, filed_date, period_end,
                primary_document, source_url, processing_status
            ) VALUES (
                %s, '0000320193', '10-Q', DATE '2025-08-01', DATE '2025-06-28',
                'q.htm', %s, 'discovered'
            )
            ON CONFLICT (accession_number) DO NOTHING
            """,
            (
                accession,
                "https://www.sec.gov/Archives/edgar/data/320193/"
                f"{accession.replace('-', '')}/q.htm",
            ),
        )
