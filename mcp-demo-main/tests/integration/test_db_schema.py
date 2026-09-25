import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors

from sec_research.db import apply_migrations


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


def test_initial_migration_is_idempotent_and_enforces_schema_contract() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    apply_migrations(TEST_DATABASE_URL)

    required_tables = {
        "companies",
        "filings",
        "filing_sections",
        "chunks",
        "index_builds",
        "chunk_embeddings",
        "xbrl_facts",
        "ingestion_runs",
        "ingestion_items",
    }

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                """
            )
        }
        assert required_tables <= tables

        embedding_type = connection.execute(
            """
            SELECT format_type(attribute.atttypid, attribute.atttypmod)
            FROM pg_attribute AS attribute
            JOIN pg_class AS relation ON relation.oid = attribute.attrelid
            WHERE relation.relname = 'chunk_embeddings'
              AND attribute.attname = 'embedding'
            """
        ).fetchone()
        assert embedding_type == ("vector(1536)",)

        active_count = connection.execute(
            "SELECT count(*) FROM index_builds WHERE is_active"
        ).fetchone()
        if active_count == (0,):
            connection.execute(
                """
                INSERT INTO index_builds (
                    index_build_id,
                    embedding_provider,
                    embedding_model,
                    embedding_dimension,
                    chunker_version,
                    status,
                    is_active
                ) VALUES (%s, 'openai', 'text-embedding-3-small', 1536, 'v1', 'ready', true)
                """,
                (uuid4(),),
            )
        assert connection.execute(
            "SELECT count(*) FROM index_builds WHERE is_active"
        ).fetchone() == (1,)
        with pytest.raises(errors.UniqueViolation):
            connection.execute(
                """
                INSERT INTO index_builds (
                    index_build_id,
                    embedding_provider,
                    embedding_model,
                    embedding_dimension,
                    chunker_version,
                    status,
                    is_active
                ) VALUES (%s, 'openai', 'text-embedding-3-small', 1536, 'v1', 'ready', true)
                """,
                (uuid4(),),
            )

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute("SET ROLE sec_query")
        company_count = connection.execute("SELECT count(*) FROM companies").fetchone()
        assert company_count is not None
        assert company_count[0] >= 0
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute(
                """
                INSERT INTO companies (cik, ticker, company_name)
                VALUES ('0000000001', 'TEST', '測試公司')
                """
            )
