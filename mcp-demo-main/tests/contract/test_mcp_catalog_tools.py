import os
from datetime import date, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors

from sec_research.db import apply_migrations
from sec_research.mcp_server import get_corpus_status_tool, list_filings_tool


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 契約測試",
)


@pytest.mark.asyncio
async def test_list_filings_filters_and_paginates_with_opaque_cursor() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    first_accession, second_accession, filed_date = _seed_filings()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        first = await list_filings_tool(
            connection,
            {
                "tickers": ["AAPL"],
                "forms": ["10-K"],
                "filed_from": filed_date,
                "filed_to": filed_date,
                "limit": 1,
                "cursor": None,
            },
        )
        second = await list_filings_tool(
            connection,
            {
                "tickers": ["AAPL"],
                "forms": ["10-K"],
                "filed_from": filed_date,
                "filed_to": filed_date,
                "limit": 1,
                "cursor": first["page"]["next_cursor"],
            },
        )

    assert first["status"] == second["status"] == "ok"
    assert first["page"]["next_cursor"] is not None
    assert second["page"]["next_cursor"] is None
    assert {
        first["data"]["filings"][0]["accession_number"],
        second["data"]["filings"][0]["accession_number"],
    } == {first_accession, second_accession}
    assert all(
        item["ticker"] == "AAPL"
        and item["form"] == "10-K"
        and item["source_url"].startswith("https://www.sec.gov/")
        for response in (first, second)
        for item in response["data"]["filings"]
    )


@pytest.mark.asyncio
async def test_list_filings_empty_and_invalid_inputs_use_contract_semantics() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        empty = await list_filings_tool(
            connection,
            {
                "tickers": ["NVDA"],
                "forms": ["10-Q"],
                "filed_from": "2001-01-01",
                "filed_to": "2001-12-31",
                "limit": 20,
                "cursor": None,
            },
        )
        invalid_payloads = [
            {"tickers": ["TSLA"]},
            {"forms": ["8-K"]},
            {"filed_from": "2024-02-30"},
            {"filed_from": "2024-02-02", "filed_to": "2024-02-01"},
            {"limit": 0},
            {"cursor": "forged"},
        ]
        invalid = [
            await list_filings_tool(connection, payload)
            for payload in invalid_payloads
        ]

    assert empty["status"] == "not_found"
    assert empty["data"] == {"filings": []}
    assert all(response["error"]["code"] == "INVALID_ARGUMENT" for response in invalid)


@pytest.mark.asyncio
async def test_corpus_status_has_counts_scope_and_active_build_without_arguments() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    _seed_filings()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        status = await get_corpus_status_tool(connection, {})
        invalid = await get_corpus_status_tool(connection, {"unexpected": True})

    assert status["status"] == "ok"
    assert {"AAPL"} <= set(status["data"]["scope"]["tickers"])
    assert {"10-K"} <= set(status["data"]["scope"]["forms"])
    assert status["data"]["counts"]["filings"] >= 2
    assert status["data"]["counts"]["sections"] >= 0
    assert status["data"]["counts"]["chunks"] >= 0
    assert "active_index_build" in status["data"]
    assert status["data"]["last_sync_at"] is not None
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"


def test_query_role_remains_read_only() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        with connection.transaction():
            connection.execute("SET LOCAL ROLE sec_query")
            connection.execute("SELECT set_config('statement_timeout', '10000', true)")
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(
                    """
                    INSERT INTO companies (cik, ticker, company_name)
                    VALUES ('0000000011', 'WRITE', '不應寫入')
                    """
                )


def _seed_filings() -> tuple[str, str, str]:
    assert TEST_DATABASE_URL is not None
    suffix = uuid4().int % 700_000 + 100_000
    accessions = (
        f"0000320193-19-{suffix:06d}",
        f"0000320193-19-{suffix + 1:06d}",
    )
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        used_dates = {
            row[0]
            for row in connection.execute(
                """
                SELECT filed_date
                FROM filings
                WHERE cik = '0000320193' AND form = '10-K'
                """
            )
        }
        candidate = date(1990, 1, 1)
        while candidate in used_dates:
            candidate += timedelta(days=1)
        connection.execute(
            """
            INSERT INTO companies (cik, ticker, company_name)
            VALUES ('0000320193', 'AAPL', 'Apple Inc.')
            ON CONFLICT (cik) DO NOTHING
            """
        )
        for accession in accessions:
            connection.execute(
                """
                INSERT INTO filings (
                    accession_number, cik, form, filed_date, period_end,
                    primary_document, source_url, processing_status
                ) VALUES (%s, '0000320193', '10-K', %s, %s, 'aapl.htm', %s, 'parsed')
                """,
                (
                    accession,
                    candidate,
                    candidate - timedelta(days=30),
                    "https://www.sec.gov/Archives/edgar/data/320193/"
                    f"{accession.replace('-', '')}/aapl.htm",
                ),
            )
    return accessions[0], accessions[1], candidate.isoformat()
