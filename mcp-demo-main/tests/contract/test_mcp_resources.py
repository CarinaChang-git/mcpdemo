import hashlib
import os
from uuid import uuid4

import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.mcp_server import get_corpus_status_tool, read_resource


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 契約測試",
)


@pytest.mark.asyncio
async def test_resources_match_tools_and_expose_only_local_verified_data() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    accession = _seed_resource_data()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        filing = await read_resource(connection, f"sec://filings/{accession}")
        section = await read_resource(
            connection,
            f"sec://filings/{accession}/sections/ITEM_1A",
        )
        status_resource = await read_resource(connection, "sec://corpus/status")
        status_tool = await get_corpus_status_tool(connection, {})

    assert filing["status"] == section["status"] == "ok"
    assert filing["data"]["accession_number"] == accession
    assert filing["data"]["sections"][0]["section_code"] == "ITEM_1A"
    assert section["data"]["content_text"] == "verified local section"
    assert section["citations"][0]["source_url"].startswith("https://www.sec.gov/")
    assert status_resource["data"] == status_tool["data"]


@pytest.mark.asyncio
async def test_resources_reject_arbitrary_or_ambiguous_urls() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        for uri in (
            "https://www.sec.gov/Archives/file.htm",
            "sec://example.com/private",
            "sec://filings/not-an-accession",
            "sec://filings/0000320193-24-000001/sections/ITEM_99",
        ):
            response = await read_resource(connection, uri)
            assert response["error"]["code"] == "INVALID_ARGUMENT"


def _seed_resource_data() -> str:
    assert TEST_DATABASE_URL is not None
    suffix = uuid4().int % 700_000 + 100_000
    accession = f"0000320193-24-{suffix:06d}"
    content = "verified local section"
    source_url = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        f"{accession.replace('-', '')}/aapl.htm"
    )
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
                %s, '0000320193', '10-K', DATE '2024-11-01', DATE '2024-09-28',
                'aapl.htm', %s, 'parsed'
            )
            """,
            (accession, source_url),
        )
        connection.execute(
            """
            INSERT INTO filing_sections (
                accession_number, section_code, section_title, ordinal,
                content_text, content_sha256, parse_confidence, parse_status
            ) VALUES (
                %s, 'ITEM_1A', 'Item 1A. Risk Factors', 1,
                %s, %s, 1, 'parsed'
            )
            """,
            (accession, content, hashlib.sha256(content.encode()).hexdigest()),
        )
    return accession
