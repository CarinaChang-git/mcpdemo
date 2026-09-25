import os
from uuid import uuid4

import psycopg
import pytest

from sec_research import cli
from sec_research.config import Settings
from sec_research.db import apply_migrations
from sec_research.ingest import (
    IngestionConflict,
    ResearchScope,
    build_intent_hash,
    checkpoint_item,
    finish_run,
    start_run,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


def test_interrupted_run_resumes_and_keeps_checkpoint_summary() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    pipeline_version = f"test-{uuid4()}"
    scope = ResearchScope.create()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        _ensure_filing(connection)
        run_id = start_run(connection, scope, pipeline_version)
        with pytest.raises(IngestionConflict):
            start_run(connection, scope, pipeline_version)

        checkpoint_item(
            connection,
            run_id,
            "0000320193-25-000079",
            stage="downloaded",
            status="completed",
        )
        failed_counts = finish_run(connection, run_id, status="failed")
        assert failed_counts == {"downloaded": 1}

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        resumed_run_id = start_run(connection, scope, pipeline_version)
        assert resumed_run_id == run_id
        checkpoint_item(
            connection,
            run_id,
            "0000320193-25-000079",
            stage="parsed",
            status="completed",
        )
        completed_counts = finish_run(
            connection, run_id, status="completed", extra_counts={"failed_sections": 2}
        )
        assert completed_counts == {"parsed": 1, "failed_sections": 2}

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        row = connection.execute(
            """
            SELECT status, counts, completed_at IS NOT NULL
            FROM ingestion_runs
            WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()
        item = connection.execute(
            """
            SELECT stage, status, attempt_count
            FROM ingestion_items
            WHERE run_id = %s AND accession_number = %s
            """,
            (run_id, "0000320193-25-000079"),
        ).fetchone()

    assert row == ("completed", {"parsed": 1, "failed_sections": 2}, True)
    assert item == ("parsed", "completed", 2)


@pytest.mark.asyncio
async def test_cli_persists_failed_run_when_sec_dependency_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    pipeline_version = f"test-{uuid4()}"
    monkeypatch.setattr(cli, "PIPELINE_VERSION", pipeline_version)

    class FailingSecClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FailingSecClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def fetch_company_mapping(self) -> dict[str, object]:
            raise RuntimeError("SEC 暫時無法使用")

    monkeypatch.setattr(cli, "SecClient", FailingSecClient)
    settings = Settings(
        sec_user_agent="sec-research-demo contact@example.com",
        database_url=TEST_DATABASE_URL,
    )

    with pytest.raises(RuntimeError, match="SEC 暫時無法使用"):
        await cli._ingest(settings, ("AAPL",), 1, "backfill")

    intent_hash = build_intent_hash(
        ResearchScope.create(("AAPL",), years=1), pipeline_version
    )
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        row = connection.execute(
            "SELECT status FROM ingestion_runs WHERE intent_hash = %s",
            (intent_hash,),
        ).fetchone()

    assert row == ("failed",)


def _ensure_filing(connection: psycopg.Connection[tuple[object, ...]]) -> None:
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
            accession_number,
            cik,
            form,
            filed_date,
            period_end,
            primary_document,
            source_url
        ) VALUES (
            '0000320193-25-000079',
            '0000320193',
            '10-Q',
            '2025-08-01',
            '2025-06-28',
            'aapl-20250628.htm',
            'https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250628.htm'
        ) ON CONFLICT (accession_number) DO NOTHING
        """
    )
