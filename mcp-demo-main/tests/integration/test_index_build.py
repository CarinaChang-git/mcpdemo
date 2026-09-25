import os
from collections.abc import Sequence
from uuid import UUID, uuid4

import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.rag import IndexBuildError, build_index


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


class FakeEmbeddings:
    def __init__(
        self,
        *,
        dimension: int = 1536,
        fail_on_call: int | None = None,
    ) -> None:
        self.dimension = dimension
        self.fail_on_call = fail_on_call
        self.calls = 0

    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        assert model == "openai/text-embedding-3-small"
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("模擬批次失敗")
        return [[float(index % 7) for index in range(self.dimension)] for _ in texts]


def test_successful_index_build_is_versioned_idempotent_and_switches_atomically() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    build_id = uuid4()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        _ensure_chunks(connection, 2)
        old_active = _ensure_active_build(connection)
        fake = FakeEmbeddings()

        first = build_index(
            connection,
            fake,
            index_build_id=build_id,
            batch_size=2,
        )
        calls_after_first = fake.calls
        second = build_index(
            connection,
            fake,
            index_build_id=build_id,
            batch_size=2,
        )
        build_row = connection.execute(
            """
            SELECT embedding_provider, embedding_model, embedding_dimension,
                   chunker_version, status, is_active
            FROM index_builds
            WHERE index_build_id = %s
            """,
            (build_id,),
        ).fetchone()
        vector_count = connection.execute(
            "SELECT count(*) FROM chunk_embeddings WHERE index_build_id = %s",
            (build_id,),
        ).fetchone()[0]
        chunk_count = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        active_ids = connection.execute(
            "SELECT index_build_id FROM index_builds WHERE is_active"
        ).fetchall()

    assert first == second
    assert first.index_build_id == build_id
    assert first.vector_count == chunk_count
    assert fake.calls == calls_after_first
    assert build_row == (
        "openrouter",
        "openai/text-embedding-3-small",
        1536,
        "v1",
        "ready",
        True,
    )
    assert vector_count == chunk_count
    assert active_ids == [(build_id,)]
    assert old_active != build_id


def test_partial_failure_marks_candidate_failed_without_switching_active_build() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    build_id = uuid4()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        _ensure_chunks(connection, 2)
        old_active = _ensure_active_build(connection)
        with pytest.raises(IndexBuildError, match="embedding 批次失敗"):
            build_index(
                connection,
                FakeEmbeddings(fail_on_call=2),
                index_build_id=build_id,
                batch_size=1,
            )
        candidate = connection.execute(
            "SELECT status, is_active FROM index_builds WHERE index_build_id = %s",
            (build_id,),
        ).fetchone()
        active = connection.execute(
            "SELECT index_build_id FROM index_builds WHERE is_active"
        ).fetchone()
        partial_count = connection.execute(
            "SELECT count(*) FROM chunk_embeddings WHERE index_build_id = %s",
            (build_id,),
        ).fetchone()[0]

    assert candidate == ("failed", False)
    assert active == (old_active,)
    assert partial_count == 1


def test_wrong_embedding_dimension_fails_without_switching_active_build() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    build_id = uuid4()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        _ensure_chunks(connection, 1)
        old_active = _ensure_active_build(connection)
        with pytest.raises(IndexBuildError, match="向量維度必須為 1536"):
            build_index(
                connection,
                FakeEmbeddings(dimension=8),
                index_build_id=build_id,
            )
        candidate = connection.execute(
            "SELECT status, is_active FROM index_builds WHERE index_build_id = %s",
            (build_id,),
        ).fetchone()
        active = connection.execute(
            "SELECT index_build_id FROM index_builds WHERE is_active"
        ).fetchone()

    assert candidate == ("failed", False)
    assert active == (old_active,)


def _ensure_chunks(
    connection: psycopg.Connection[tuple[object, ...]],
    count: int,
) -> None:
    suffix = uuid4().int % 800_000 + 100_000
    accession = f"0000320193-24-{suffix:06d}"
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
            'test.htm', %s, 'parsed'
        )
        """,
        (
            accession,
            "https://www.sec.gov/Archives/edgar/data/320193/"
            f"{accession.replace('-', '')}/test.htm",
        ),
    )
    section_id = connection.execute(
        """
        INSERT INTO filing_sections (
            accession_number, section_code, section_title, ordinal,
            content_text, content_sha256, parse_confidence, parse_status
        ) VALUES (%s, 'ITEM_1', 'Business', 0, '測試章節', %s, 1, 'parsed')
        RETURNING section_id
        """,
        (accession, "a" * 64),
    ).fetchone()[0]
    for index in range(count):
        connection.execute(
            """
            INSERT INTO chunks (
                section_id, chunk_index, content_text, token_count, content_sha256
            ) VALUES (%s, %s, %s, 2, %s)
            """,
            (section_id, index, f"測試 chunk {index} {uuid4()}", uuid4().hex * 2),
        )
    connection.commit()


def _ensure_active_build(
    connection: psycopg.Connection[tuple[object, ...]],
) -> UUID:
    row = connection.execute(
        "SELECT index_build_id FROM index_builds WHERE is_active"
    ).fetchone()
    if row:
        return row[0]
    build_id = uuid4()
    connection.execute(
        """
        INSERT INTO index_builds (
            index_build_id, embedding_provider, embedding_model,
            embedding_dimension, chunker_version, status, is_active
        ) VALUES (%s, 'openai', 'text-embedding-3-small', 1536, 'v1', 'ready', true)
        """,
        (build_id,),
    )
    connection.commit()
    return build_id
