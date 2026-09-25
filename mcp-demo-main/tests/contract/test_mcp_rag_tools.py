import hashlib
import json
import logging
import os
from collections.abc import Sequence
from uuid import uuid4

import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.mcp_server import read_filing_section_tool, search_filing_sections_tool


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 契約測試",
)


class QueryEmbeddings:
    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        assert texts and model == "openai/text-embedding-3-small"
        return [[1.0] + [0.0] * 1535 for _ in texts]


@pytest.mark.asyncio
async def test_search_maps_hybrid_ranks_neighbors_metadata_and_citations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    accession = _seed_rag_data()

    with caplog.at_level(logging.INFO, logger="sec_research.mcp_server"):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            response = await search_filing_sections_tool(
                connection,
                QueryEmbeddings(),
                {
                    "query": "supply chain risk",
                    "tickers": ["AAPL"],
                    "forms": ["10-K"],
                    "filed_from": "2024-01-01",
                    "filed_to": "2024-12-31",
                    "sections": ["ITEM_1A"],
                    "top_k": 2,
                },
            )

    assert response["status"] == "ok"
    assert len(response["data"]["matches"]) == 2
    assert len(response["citations"]) == 2
    assert {match["accession_number"] for match in response["data"]["matches"]} == {
        accession
    }
    assert [match["fusion_rank"] for match in response["data"]["matches"]] == [1, 2]
    assert any(
        match["next_chunk_id"] is not None
        for match in response["data"]["matches"]
    )
    assert any(
        match["previous_chunk_id"] is not None
        for match in response["data"]["matches"]
    )
    assert all(
        citation["source_url"].startswith("https://www.sec.gov/")
        for citation in response["citations"]
    )
    assert all(
        citation["excerpt"].startswith("supply chain risk")
        and len(citation["excerpt"]) <= 500
        for citation in response["citations"]
    )
    event = next(
        json.loads(record.message)
        for record in caplog.records
        if '"operation":"SearchFilingSectionsInput"' in record.message
    )
    assert event["request_id"] == response["request_id"]
    assert event["status"] == "ok"
    assert event["result_count"] == 2
    assert event["duration_ms"] >= 0
    assert event["error_code"] is None
    assert "query" not in event


@pytest.mark.asyncio
async def test_search_treats_sql_metacharacters_as_query_data() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    _seed_rag_data()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        response = await search_filing_sections_tool(
            connection,
            QueryEmbeddings(),
            {"query": "risk'; DROP TABLE filings; --"},
        )
        filings_table = connection.execute(
            "SELECT to_regclass('public.filings')"
        ).fetchone()

    assert response["status"] == "ok"
    assert filings_table == ("filings",)


@pytest.mark.asyncio
async def test_read_section_is_exact_paginated_and_cited() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    accession = _seed_rag_data(section_text="x" * 2500)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        first = await read_filing_section_tool(
            connection,
            {
                "accession_number": accession,
                "section_code": "ITEM_1A",
                "cursor": None,
                "max_chars": 1000,
            },
        )
        second = await read_filing_section_tool(
            connection,
            {
                "accession_number": accession,
                "section_code": "ITEM_1A",
                "cursor": first["page"]["next_cursor"],
                "max_chars": 1000,
            },
        )
        missing = await read_filing_section_tool(
            connection,
            {
                "accession_number": accession,
                "section_code": "ITEM_8",
                "cursor": None,
                "max_chars": 1000,
            },
        )

    assert first["status"] == second["status"] == "ok"
    assert len(first["data"]["content_text"]) == 1000
    assert len(second["data"]["content_text"]) == 1000
    assert first["page"]["next_cursor"] is not None
    assert second["page"]["next_cursor"] is not None
    assert first["citations"][0]["accession_number"] == accession
    assert missing["status"] == "not_found"


@pytest.mark.asyncio
async def test_rag_tools_reject_ambiguous_or_invalid_parameters() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    accession = _seed_rag_data()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        invalid_searches = [
            await search_filing_sections_tool(
                connection, QueryEmbeddings(), payload
            )
            for payload in (
                {"query": ""},
                {"query": "x" * 2001},
                {"query": "risk", "tickers": ["TSLA"]},
                {"query": "risk", "forms": ["8-K"]},
                {"query": "risk", "top_k": 13},
            )
        ]
        invalid_reads = [
            await read_filing_section_tool(connection, payload)
            for payload in (
                {
                    "accession_number": "bad",
                    "section_code": "ITEM_1A",
                    "max_chars": 1000,
                },
                {
                    "accession_number": accession,
                    "section_code": "ITEM_99",
                    "max_chars": 1000,
                },
                {
                    "accession_number": accession,
                    "section_code": "ITEM_1A",
                    "max_chars": 1000,
                    "cursor": "forged",
                },
            )
        ]

    assert all(
        response["error"]["code"] == "INVALID_ARGUMENT"
        for response in invalid_searches + invalid_reads
    )


def _seed_rag_data(section_text: str | None = None) -> str:
    assert TEST_DATABASE_URL is not None
    suffix = uuid4().int % 700_000 + 100_000
    accession = f"0000320193-24-{suffix:06d}"
    build_id = uuid4()
    source_url = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        f"{accession.replace('-', '')}/aapl.htm"
    )
    content = section_text or "supply chain risk one\nsupply chain risk two"
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        connection.execute("UPDATE index_builds SET is_active = false WHERE is_active")
        connection.execute(
            """
            INSERT INTO index_builds (
                index_build_id, embedding_provider, embedding_model,
                embedding_dimension, chunker_version, status, is_active
            ) VALUES (%s, 'openrouter', 'openai/text-embedding-3-small', 1536, 'v1', 'ready', true)
            """,
            (build_id,),
        )
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
                'aapl.htm', %s, 'indexed'
            )
            """,
            (accession, source_url),
        )
        section_digest = hashlib.sha256(content.encode()).hexdigest()
        section_id = connection.execute(
            """
            INSERT INTO filing_sections (
                accession_number, section_code, section_title, ordinal,
                content_text, content_sha256, parse_confidence, parse_status
            ) VALUES (
                %s, 'ITEM_1A', 'Item 1A. Risk Factors', 1,
                %s, %s, 1, 'parsed'
            ) RETURNING section_id
            """,
            (accession, content, section_digest),
        ).fetchone()[0]
        vector = "[1," + ",".join("0" for _ in range(1535)) + "]"
        for index, chunk_text in enumerate(
            ("supply chain risk factories", "supply chain risk suppliers")
        ):
            chunk_id = uuid4()
            digest = hashlib.sha256(chunk_text.encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO chunks (
                    chunk_id, section_id, chunk_index, content_text,
                    token_count, content_sha256
                ) VALUES (%s, %s, %s, %s, 4, %s)
                """,
                (chunk_id, section_id, index, chunk_text, digest),
            )
            connection.execute(
                """
                INSERT INTO chunk_embeddings (
                    chunk_id, index_build_id, chunk_text_sha256, embedding
                ) VALUES (%s, %s, %s, %s::vector)
                """,
                (chunk_id, build_id, digest, vector),
            )
    return accession
