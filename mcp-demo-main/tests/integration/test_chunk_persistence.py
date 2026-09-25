import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.ingest import FilingMetadata
from sec_research.parser import parse_filing
from sec_research.rag import persist_sections_and_chunks


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
FIXTURE = Path(__file__).parents[1] / "fixtures" / "sec" / "10k.html"
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


def test_section_and_chunk_persistence_is_idempotent() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    suffix = uuid4().int % 800_000 + 100_000
    accession = f"0000320193-24-{suffix:06d}"
    filing = FilingMetadata.create(
        cik="0000320193",
        ticker="AAPL",
        company_name="Apple Inc.",
        form="10-K",
        filed_date="2024-11-01",
        period_end="2024-09-28",
        accession_number=accession,
        primary_document="aapl-20240928.htm",
        source_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/"
            f"{accession.replace('-', '')}/aapl-20240928.htm"
        ),
    )
    parsed = parse_filing(FIXTURE.read_bytes(), "10-K")
    assert not parsed.failures

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        _ensure_filing(connection, filing)
        first = persist_sections_and_chunks(connection, filing, parsed.sections)
        second = persist_sections_and_chunks(connection, filing, parsed.sections)

    assert first == {"sections": 5, "chunks": 5}
    assert second == first

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        section_count = connection.execute(
            "SELECT count(*) FROM filing_sections WHERE accession_number = %s",
            (accession,),
        ).fetchone()
        chunk_count = connection.execute(
            """
            SELECT count(*)
            FROM chunks
            JOIN filing_sections USING (section_id)
            WHERE accession_number = %s
            """,
            (accession,),
        ).fetchone()
        ordered = connection.execute(
            """
            SELECT filing_sections.section_code, chunks.chunk_index
            FROM chunks
            JOIN filing_sections USING (section_id)
            WHERE accession_number = %s
            ORDER BY filing_sections.ordinal, chunks.chunk_index
            """,
            (accession,),
        ).fetchall()

    assert section_count == (5,)
    assert chunk_count == (5,)
    assert ordered == [
        ("ITEM_1", 0),
        ("ITEM_1A", 0),
        ("ITEM_1C", 0),
        ("ITEM_7", 0),
        ("ITEM_8", 0),
    ]


def _ensure_filing(
    connection: psycopg.Connection[tuple[object, ...]],
    filing: FilingMetadata,
) -> None:
    connection.execute(
        """
        INSERT INTO companies (cik, ticker, company_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (cik) DO NOTHING
        """,
        (filing.cik, filing.ticker, filing.company_name),
    )
    connection.execute(
        """
        INSERT INTO filings (
            accession_number,
            cik,
            form,
            filed_date,
            period_end,
            primary_document,
            source_url,
            processing_status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'downloaded')
        """,
        (
            filing.accession_number,
            filing.cik,
            filing.form,
            filing.filed_date,
            filing.period_end,
            filing.primary_document,
            filing.source_url,
        ),
    )
