import asyncio
from contextlib import nullcontext
from datetime import date
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from sec_research import cli
from sec_research import mcp_server
from sec_research.config import Settings
from sec_research.rag import IndexBuildResult
from sec_research.parser import ParseFailure, ParseResult


build_parser = cli.build_parser


def test_cli_exposes_only_approved_operational_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["migrate"]).command == "migrate"
    assert parser.parse_args(["backfill"]).years == 5
    assert parser.parse_args(["sync"]).years == 5
    assert parser.parse_args(["rebuild-index"]).command == "rebuild-index"


def test_rebuild_index_uses_only_openrouter_key_for_embeddings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        sec_user_agent="sec-research-demo contact@example.com",
        database_url="postgresql://postgres:placeholder@localhost/sec_research",
        openrouter_api_key="placeholder-router-key",
    )
    arguments: dict[str, str] = {}

    def fake_openai(**kwargs: str) -> object:
        arguments.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "OpenAI", fake_openai)
    class FakeConnection:
        def execute(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(fetchone=lambda: (57, 195, 1703, 0))

    monkeypatch.setattr(cli.psycopg, "connect", lambda _: nullcontext(FakeConnection()))
    monkeypatch.setattr(
        cli,
        "build_index",
        lambda connection, embeddings: IndexBuildResult(UUID(int=1), 0),
    )
    printed: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_print", printed.append)

    with caplog.at_level(logging.INFO, logger="sec_research.ingest"):
        cli._rebuild_index(settings)

    assert arguments == {
        "api_key": "placeholder-router-key",
        "base_url": "https://openrouter.ai/api/v1",
    }
    event = json.loads(caplog.records[-1].message)
    assert event["operation"] == "rebuild_index"
    assert event["status"] == "completed"
    assert event["result_count"] == 0
    assert event["duration_ms"] >= 0
    assert printed[0]["corpus_summary"] == {
        "filings": 57,
        "successful_sections": 195,
        "failed_sections": 0,
        "failed_sections_scope": "latest_completed_ingestion_run",
        "chunks": 1703,
        "index_version": str(UUID(int=1)),
    }


def test_mcp_query_embeddings_use_openrouter_endpoint(monkeypatch) -> None:
    settings = Settings(
        sec_user_agent="sec-research-demo contact@example.com",
        database_url="postgresql://postgres:placeholder@localhost/sec_research",
        openrouter_api_key="placeholder-router-key",
    )
    arguments: dict[str, str] = {}

    def fake_openai(**kwargs: str) -> object:
        arguments.update(kwargs)
        return object()

    class FakeServer:
        def run(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(mcp_server, "Settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "OpenAI", fake_openai)
    monkeypatch.setattr(mcp_server, "SecClient", lambda *a, **kw: object())
    monkeypatch.setattr(mcp_server, "create_mcp_server", lambda *a, **kw: FakeServer())

    mcp_server.main()

    assert arguments == {
        "api_key": "placeholder-router-key",
        "base_url": "https://openrouter.ai/api/v1",
    }


def test_embedding_entrypoints_reject_missing_openrouter_key(monkeypatch) -> None:
    settings = Settings(
        sec_user_agent="sec-research-demo contact@example.com",
        database_url="postgresql://postgres:placeholder@localhost/sec_research",
    )
    monkeypatch.setattr(mcp_server, "Settings", lambda: settings)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        cli._rebuild_index(settings)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        mcp_server.main()


def test_backfill_summary_counts_failed_sections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    filing = SimpleNamespace(
        cik="0000320193",
        accession_number="0000320193-25-000079",
        form="10-K",
        period_end=date(2025, 9, 27),
    )
    raw = tmp_path / "data/raw" / filing.cik / filing.accession_number / "filing.html"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"<html></html>")
    connection = SimpleNamespace(execute=lambda *args, **kwargs: None)

    class FakeSecClient:
        async def __aenter__(self) -> "FakeSecClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def fetch_company_mapping(self) -> dict[str, object]:
            return {}

        async def fetch_submissions(self, cik: str) -> dict[str, object]:
            return {}

    async def fake_download(*args: object) -> dict[str, int]:
        return {"added": 1}

    async def fake_facts(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(inserted=0, updated=0)

    monkeypatch.setattr(cli.psycopg, "connect", lambda *args, **kwargs: nullcontext(connection))
    monkeypatch.setattr(cli, "SecClient", lambda *args, **kwargs: FakeSecClient())
    monkeypatch.setattr(cli, "start_run", lambda *args: UUID(int=1))
    finished: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "finish_run", lambda *args, **kwargs: finished.append(kwargs))
    monkeypatch.setattr(cli, "resolve_company", lambda *args: SimpleNamespace(cik=filing.cik))
    monkeypatch.setattr(cli, "discover_filings", lambda *args: [filing])
    monkeypatch.setattr(cli, "download_filings", fake_download)
    monkeypatch.setattr(cli, "ingest_company_facts", fake_facts)
    monkeypatch.setattr(
        cli,
        "parse_filing",
        lambda *args: ParseResult(
            (),
            (ParseFailure("ITEM_1", "缺章"), ParseFailure("ITEM_7", "缺章")),
        ),
    )
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_print", captured.append)
    settings = Settings(
        sec_user_agent="sec-research-demo contact@example.com",
        database_url="postgresql://postgres:placeholder@localhost/sec_research",
    )

    with caplog.at_level(logging.INFO, logger="sec_research.ingest"):
        asyncio.run(cli._ingest(settings, ["AAPL"], 5, "backfill"))

    assert captured[0]["counts"]["failed_sections"] == 2
    assert captured[0]["counts"]["quarantined"] == 1
    assert finished[0]["extra_counts"] == {
        "successful_sections": 0,
        "failed_sections": 2,
        "chunks": 0,
    }
    event = json.loads(caplog.records[-1].message)
    assert event["operation"] == "parse_filing"
    assert event["status"] == "failed"
    assert event["error_code"] == "PARSE_FAILED"
    assert event["result_count"] == 2
