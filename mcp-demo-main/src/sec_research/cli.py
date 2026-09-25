"""資料建置與索引維運的明確命令列入口。"""

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from openai import OpenAI
import psycopg

from sec_research.config import Settings
from sec_research.db import apply_migrations
from sec_research.ingest import (
    RawStore,
    ResearchScope,
    discover_filings,
    download_filings,
    finish_run,
    ingest_company_facts,
    start_run,
    _log_event,
)
from sec_research.parser import parse_filing
from sec_research.rag import (
    OpenAIEmbeddingBoundary,
    build_index,
    persist_sections_and_chunks,
)
from sec_research.sec_client import SecClient, resolve_company


PIPELINE_VERSION = "v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SEC filing research 維運 CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="套用可重跑的 PostgreSQL migration")
    for name in ("backfill", "sync"):
        command = commands.add_parser(name, help="下載、解析並寫入 SEC corpus")
        command.add_argument("--years", type=int, choices=range(1, 6), default=5)
        command.add_argument(
            "--tickers",
            nargs="+",
            choices=("AAPL", "MSFT", "NVDA"),
            default=("AAPL", "MSFT", "NVDA"),
        )
    commands.add_parser("rebuild-index", help="建立並原子切換 embedding index")
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("sec_research").setLevel(logging.INFO)
    args = build_parser().parse_args(argv)
    settings = Settings()
    if args.command == "migrate":
        apply_migrations(settings.database_url)
        _print({"status": "completed", "command": "migrate"})
    elif args.command == "rebuild-index":
        _rebuild_index(settings)
    else:
        asyncio.run(_ingest(settings, args.tickers, args.years, args.command))


async def _ingest(
    settings: Settings,
    tickers: list[str] | tuple[str, ...],
    years: int,
    command: str,
) -> None:
    scope = ResearchScope.create(tickers=tickers, years=years)
    raw_root = Path("data/raw")
    totals = {
        name: 0
        for name in (
            "added",
            "updated",
            "skipped",
            "quarantined",
            "failed",
            "sections",
            "failed_sections",
            "chunks",
            "facts_inserted",
            "facts_updated",
        )
    }
    dsn = settings.database_url.get_secret_value()
    with psycopg.connect(dsn, autocommit=True) as connection:
        run_id = start_run(connection, scope, PIPELINE_VERSION)
        try:
            async with SecClient(
                settings.sec_user_agent,
                requests_per_second=settings.sec_requests_per_second,
                max_retries=3,
            ) as client:
                mapping = await client.fetch_company_mapping()
                for ticker in scope.tickers:
                    company = resolve_company(mapping, ticker)
                    submissions = await client.fetch_submissions(company.cik)
                    filings = discover_filings(submissions, scope)
                    summary = await download_filings(
                        connection,
                        run_id,
                        client,
                        RawStore(raw_root),
                        company,
                        filings,
                    )
                    for key, value in summary.items():
                        totals[key] += value
                    for filing in filings:
                        raw_path = (
                            raw_root
                            / filing.cik
                            / filing.accession_number
                            / "filing.html"
                        )
                        if not raw_path.is_file():
                            continue
                        parse_started = time.perf_counter()
                        try:
                            parsed = parse_filing(
                                raw_path.read_bytes(), filing.form, filing.period_end
                            )
                        except Exception as error:
                            _log_event(
                                operation="parse_filing",
                                status="failed",
                                request_id=str(run_id),
                                duration_ms=round((time.perf_counter() - parse_started) * 1000),
                                error_code=type(error).__name__,
                            )
                            raise
                        _log_event(
                            operation="parse_filing",
                            status="failed" if parsed.failures else "completed",
                            request_id=str(run_id),
                            duration_ms=round((time.perf_counter() - parse_started) * 1000),
                            result_count=len(parsed.failures or parsed.sections),
                            error_code="PARSE_FAILED" if parsed.failures else None,
                        )
                        if parsed.failures:
                            totals["quarantined"] += 1
                            totals["failed_sections"] += len(parsed.failures)
                            connection.execute(
                                """
                                UPDATE filings
                                SET processing_status = 'quarantined',
                                    error_code = 'PARSE_FAILED',
                                    updated_at = now()
                                WHERE accession_number = %s
                                """,
                                (filing.accession_number,),
                            )
                            continue
                        persisted = persist_sections_and_chunks(
                            connection, filing, parsed.sections
                        )
                        totals["sections"] += persisted["sections"]
                        totals["chunks"] += persisted["chunks"]
                        connection.execute(
                            """
                            UPDATE filings
                            SET processing_status = 'parsed',
                                parser_version = %s,
                                error_code = NULL,
                                updated_at = now()
                            WHERE accession_number = %s
                            """,
                            (PIPELINE_VERSION, filing.accession_number),
                        )
                    facts = await ingest_company_facts(
                        connection,
                        client,
                        company,
                        allowed_accessions={
                            filing.accession_number for filing in filings
                        },
                    )
                    totals["facts_inserted"] += facts.inserted
                    totals["facts_updated"] += facts.updated
            finish_run(
                connection, run_id, status="completed",
                extra_counts={
                    "successful_sections": totals["sections"],
                    "failed_sections": totals["failed_sections"],
                    "chunks": totals["chunks"],
                },
            )
        except Exception:
            finish_run(connection, run_id, status="failed")
            raise
    _print(
        {
            "status": "completed",
            "command": command,
            "run_id": str(run_id),
            "counts": totals,
        }
    )


def _rebuild_index(settings: Settings) -> None:
    if settings.openrouter_api_key is None or not settings.openrouter_api_key.get_secret_value():
        raise ValueError("OPENROUTER_API_KEY 為建立索引的必要設定")
    started_at = time.perf_counter()
    request_id = str(uuid4())
    try:
        client = OpenAI(
            api_key=settings.openrouter_api_key.get_secret_value(),
            base_url="https://openrouter.ai/api/v1",
        )
        with psycopg.connect(settings.database_url.get_secret_value()) as connection:
            result = build_index(connection, OpenAIEmbeddingBoundary(client))
            corpus_counts = connection.execute(
                """
                SELECT (SELECT count(*) FROM filings),
                       (SELECT count(*) FROM filing_sections),
                       (SELECT count(*) FROM chunks),
                       (SELECT (counts->>'failed_sections')::integer
                        FROM ingestion_runs
                        WHERE status = 'completed' AND counts ? 'failed_sections'
                        ORDER BY completed_at DESC LIMIT 1)
                """
            ).fetchone()
    except Exception as error:
        _log_event(
            operation="rebuild_index",
            status="failed",
            request_id=request_id,
            duration_ms=round((time.perf_counter() - started_at) * 1000),
            error_code=type(error).__name__,
        )
        raise
    _log_event(
        operation="rebuild_index",
        status="completed",
        request_id=request_id,
        duration_ms=round((time.perf_counter() - started_at) * 1000),
        result_count=result.vector_count,
    )
    _print({
        "status": "completed", "command": "rebuild-index", **asdict(result),
        "corpus_summary": {
            "filings": corpus_counts[0],
            "successful_sections": corpus_counts[1],
            "failed_sections": corpus_counts[3],
            "failed_sections_scope": "latest_completed_ingestion_run",
            "chunks": corpus_counts[2],
            "index_version": str(result.index_build_id),
        },
    })


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")))


if __name__ == "__main__":
    main()
