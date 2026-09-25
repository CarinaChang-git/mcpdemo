import os
from collections.abc import Sequence
from datetime import date
from uuid import UUID, uuid4

import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.rag import hybrid_search


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


class QueryEmbeddings:
    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        assert texts and model == "openai/text-embedding-3-small"
        return [[1.0] + [0.0] * 1535 for _ in texts]


def test_hybrid_search_filters_before_rrf_and_paginates_stably() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        active_build, inactive_build, indexed_chunks = _seed_retrieval_data(connection)
        first = hybrid_search(
            connection,
            QueryEmbeddings(),
            "supply chain risk",
            tickers=["aapl"],
            forms=["10-k"],
            period_from=date(2020, 9, 26),
            period_to=date(2020, 9, 26),
            sections=["item_1a"],
            top_k=1,
        )
        repeated = hybrid_search(
            connection,
            QueryEmbeddings(),
            "supply chain risk",
            tickers=["AAPL"],
            forms=["10-K"],
            period_from="2020-09-26",
            period_to="2020-09-26",
            sections=["ITEM_1A"],
            top_k=1,
        )
        second = hybrid_search(
            connection,
            QueryEmbeddings(),
            "supply chain risk",
            tickers=["AAPL"],
            forms=["10-K"],
            period_from="2020-09-26",
            period_to="2020-09-26",
            sections=["ITEM_1A"],
            top_k=1,
            cursor=first.next_cursor,
        )

    assert first == repeated
    assert first.active_index_build_id == active_build
    assert first.next_cursor is not None
    assert second.next_cursor is None
    assert {first.results[0].chunk_id, second.results[0].chunk_id} == set(
        indexed_chunks
    )
    assert all(
        result.ticker == "AAPL"
        and result.form == "10-K"
        and result.period_end == date(2020, 9, 26)
        and result.section_code == "ITEM_1A"
        and result.index_build_id == active_build
        and result.fusion_rank >= 1
        and (result.keyword_rank is not None or result.vector_rank is not None)
        for result in first.results + second.results
    )
    assert inactive_build != active_build


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"tickers": ["TSLA"]}, "tickers"),
        ({"forms": ["8-K"]}, "forms"),
        ({"sections": ["ITEM_99"]}, "sections"),
        ({"top_k": 0}, "top_k"),
        ({"period_from": "2024-02-30"}, "日期"),
        (
            {"period_from": "2024-02-01", "period_to": "2024-01-01"},
            "日期範圍",
        ),
        ({"cursor": "not-a-cursor"}, "cursor"),
    ],
)
def test_hybrid_search_rejects_invalid_filters(
    arguments: dict[str, object],
    message: str,
) -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        _seed_retrieval_data(connection)
        with pytest.raises(ValueError, match=message):
            hybrid_search(
                connection,
                QueryEmbeddings(),
                "risk",
                **arguments,
            )


def _seed_retrieval_data(
    connection: psycopg.Connection[tuple[object, ...]],
) -> tuple[UUID, UUID, tuple[UUID, UUID]]:
    connection.execute("UPDATE index_builds SET is_active = false WHERE is_active")
    active_build = uuid4()
    inactive_build = uuid4()
    for build_id, active in ((active_build, True), (inactive_build, False)):
        connection.execute(
            """
            INSERT INTO index_builds (
                index_build_id, embedding_provider, embedding_model,
                embedding_dimension, chunker_version, status, is_active
            ) VALUES (%s, 'openrouter', 'openai/text-embedding-3-small', 1536, 'v1', 'ready', %s)
            """,
            (build_id, active),
        )
    connection.execute(
        """
        INSERT INTO companies (cik, ticker, company_name)
        VALUES ('0000320193', 'AAPL', 'Apple Inc.')
        ON CONFLICT (cik) DO NOTHING
        """
    )
    suffix = uuid4().int % 800_000 + 100_000
    accession = f"0000320193-20-{suffix:06d}"
    connection.execute(
        """
        INSERT INTO filings (
            accession_number, cik, form, filed_date, period_end,
            primary_document, source_url, processing_status
        ) VALUES (
            %s, '0000320193', '10-K', DATE '2020-10-30', DATE '2020-09-26',
            'aapl.htm', %s, 'indexed'
        )
        """,
        (
            accession,
            "https://www.sec.gov/Archives/edgar/data/320193/"
            f"{accession.replace('-', '')}/aapl.htm",
        ),
    )
    section_id = connection.execute(
        """
        INSERT INTO filing_sections (
            accession_number, section_code, section_title, ordinal,
            content_text, content_sha256, parse_confidence, parse_status
        ) VALUES (
            %s, 'ITEM_1A', 'Item 1A. Risk Factors', 1,
            'Supply chain risk section', %s, 1, 'parsed'
        )
        RETURNING section_id
        """,
        (accession, uuid4().hex * 2),
    ).fetchone()[0]
    chunk_ids: list[UUID] = []
    vector = "[1," + ",".join("0" for _ in range(1535)) + "]"
    for index, content in enumerate(
        (
            "supply chain risk concentration factories",
            "supply chain risk logistics suppliers",
            "supply chain risk inactive-only evidence",
        )
    ):
        chunk_id = uuid4()
        digest = uuid4().hex * 2
        chunk_ids.append(chunk_id)
        connection.execute(
            """
            INSERT INTO chunks (
                chunk_id, section_id, chunk_index, content_text,
                token_count, content_sha256
            ) VALUES (%s, %s, %s, %s, 5, %s)
            """,
            (chunk_id, section_id, index, content, digest),
        )
        build_id = active_build if index < 2 else inactive_build
        connection.execute(
            """
            INSERT INTO chunk_embeddings (
                chunk_id, index_build_id, chunk_text_sha256, embedding
            ) VALUES (%s, %s, %s, %s::vector)
            """,
            (chunk_id, build_id, digest, vector),
        )
    connection.commit()
    return active_build, inactive_build, (chunk_ids[0], chunk_ids[1])
