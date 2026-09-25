"""Ingestion scope、可續跑 checkpoint 與原始 SEC 檔案保存。"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from sec_research.sec_client import (
    Company,
    SecClient,
    SecClientError,
    build_filing_url,
    normalize_accession,
    normalize_cik,
    validate_sec_url,
)


ALLOWED_TICKERS = frozenset({"AAPL", "MSFT", "NVDA"})
ALLOWED_FORMS = frozenset({"10-K", "10-Q"})
ITEM_STAGES = frozenset(
    {"discovered", "downloaded", "parsed", "indexed", "failed", "quarantined"}
)
ITEM_STATUSES = frozenset({"pending", "running", "completed", "failed"})
SUPPORTED_XBRL_CONCEPTS = frozenset(
    {
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "ResearchAndDevelopmentExpense",
        "NetIncomeLoss",
        "Assets",
    }
)
ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
logger = logging.getLogger(__name__)


class IngestionConflict(RuntimeError):
    """相同 intent 已有執行中的 run。"""


class RawContentConflict(RuntimeError):
    """相同 accession 已保存不同內容。"""


class FilingIdentityError(RuntimeError):
    """Filing metadata、來源 URL 或內容 identity 不一致。"""


@dataclass(frozen=True, slots=True)
class ResearchScope:
    tickers: tuple[str, ...]
    forms: tuple[str, ...]
    years: int

    @classmethod
    def create(
        cls,
        tickers: list[str] | tuple[str, ...] | None = None,
        forms: list[str] | tuple[str, ...] | None = None,
        years: int = 5,
    ) -> "ResearchScope":
        normalized_tickers = tuple(
            sorted({ticker.strip().upper() for ticker in tickers or ALLOWED_TICKERS})
        )
        normalized_forms = tuple(
            sorted({form.strip().upper() for form in forms or ALLOWED_FORMS})
        )
        if not normalized_tickers or not set(normalized_tickers) <= ALLOWED_TICKERS:
            raise ValueError("tickers 必須是 AAPL、MSFT、NVDA 的非空子集合")
        if not normalized_forms or not set(normalized_forms) <= ALLOWED_FORMS:
            raise ValueError("forms 必須是 10-K、10-Q 的非空子集合")
        if not 1 <= years <= 5:
            raise ValueError("years 必須介於 1 與 5")
        return cls(normalized_tickers, normalized_forms, years)

    def as_dict(self) -> dict[str, object]:
        return {
            "tickers": list(self.tickers),
            "forms": list(self.forms),
            "years": self.years,
        }


@dataclass(frozen=True, slots=True)
class RawWriteResult:
    path: Path
    sha256: str
    created: bool


@dataclass(frozen=True, slots=True)
class FilingMetadata:
    cik: str
    ticker: str
    company_name: str
    form: str
    filed_date: date
    period_end: date
    accession_number: str
    primary_document: str
    source_url: str

    @classmethod
    def create(
        cls,
        *,
        cik: str | int,
        ticker: str,
        company_name: str,
        form: str,
        filed_date: str | date,
        period_end: str | date,
        accession_number: str,
        primary_document: str,
        source_url: str,
    ) -> "FilingMetadata":
        normalized_cik = normalize_cik(cik)
        accession = normalize_accession(accession_number)
        expected_url = build_filing_url(
            normalized_cik,
            accession,
            primary_document,
        )
        if validate_sec_url(source_url) != expected_url:
            raise ValueError("filing source URL 與 identity 不一致")
        normalized_form = form.strip().upper()
        if normalized_form not in ALLOWED_FORMS:
            raise ValueError("filing form 不在 corpus 範圍")
        return cls(
            cik=normalized_cik,
            ticker=ticker.strip().upper(),
            company_name=company_name.strip(),
            form=normalized_form,
            filed_date=(
                filed_date if isinstance(filed_date, date) else date.fromisoformat(filed_date)
            ),
            period_end=(
                period_end if isinstance(period_end, date) else date.fromisoformat(period_end)
            ),
            accession_number=accession,
            primary_document=primary_document,
            source_url=expected_url,
        )


@dataclass(frozen=True, slots=True)
class XbrlFact:
    cik: str
    accession_number: str
    taxonomy: str
    concept: str
    unit: str
    value: Decimal
    start_date: date | None
    end_date: date
    fiscal_year: int
    fiscal_period: str
    form: str
    filed_date: date
    frame: str | None

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.cik,
            self.taxonomy,
            self.concept,
            self.unit,
            self.start_date,
            self.end_date,
            self.form,
            self.filed_date,
            self.accession_number,
        )


@dataclass(frozen=True, slots=True)
class XbrlIngestionSummary:
    inserted: int
    updated: int
    duplicates: int
    skipped: int
    warnings: tuple[str, ...]


class RawStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(
        self,
        cik: str | int,
        accession_number: str,
        content: bytes,
    ) -> RawWriteResult:
        normalized_cik = normalize_cik(cik)
        accession = normalize_accession(accession_number)
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / normalized_cik / accession / "filing.html"
        if target.exists():
            existing_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing_digest != digest:
                raise RawContentConflict("相同 accession 已存在不同 SHA-256")
            return RawWriteResult(target, digest, False)

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=".tmp-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return RawWriteResult(target, digest, True)


def discover_filings(
    submissions: dict[str, Any],
    scope: ResearchScope,
) -> list[FilingMetadata]:
    """由 submissions recent 清單選出最近已完成會計年度的 corpus filing。"""
    try:
        recent = submissions["filings"]["recent"]
        columns = {
            name: recent[name]
            for name in (
                "accessionNumber",
                "filingDate",
                "reportDate",
                "form",
                "primaryDocument",
            )
        }
    except (KeyError, TypeError) as error:
        raise ValueError("submissions recent schema 無效") from error
    lengths = {len(values) for values in columns.values() if isinstance(values, list)}
    if len(lengths) != 1 or len(columns) != sum(
        isinstance(values, list) for values in columns.values()
    ):
        raise ValueError("submissions recent 欄位長度不一致")

    records = [
        dict(zip(columns, values, strict=True))
        for values in zip(*columns.values(), strict=True)
    ]
    completed_years = sorted(
        {
            date.fromisoformat(record["reportDate"]).year
            for record in records
            if record["form"] == "10-K"
        },
        reverse=True,
    )[: scope.years]
    if not completed_years:
        return []

    try:
        cik = normalize_cik(submissions["cik"])
        tickers = submissions["tickers"]
        company_name = str(submissions["name"])
        ticker = str(tickers[0])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError("submissions company identity 無效") from error
    filings: list[FilingMetadata] = []
    for record in records:
        form = str(record["form"]).upper()
        if form.endswith("/A") or form not in scope.forms:
            continue
        report_date = date.fromisoformat(record["reportDate"])
        if report_date.year not in completed_years:
            continue
        accession = str(record["accessionNumber"])
        primary_document = str(record["primaryDocument"])
        filings.append(
            FilingMetadata.create(
                cik=cik,
                ticker=ticker,
                company_name=company_name,
                form=form,
                filed_date=str(record["filingDate"]),
                period_end=report_date,
                accession_number=accession,
                primary_document=primary_document,
                source_url=build_filing_url(cik, accession, primary_document),
            )
        )
    return sorted(filings, key=lambda filing: (filing.filed_date, filing.accession_number))


async def download_filings(
    connection: psycopg.Connection[Any],
    run_id: UUID,
    client: SecClient,
    raw_store: RawStore,
    company: Company,
    filings: list[FilingMetadata],
) -> dict[str, int]:
    """下載一組已發現 filing，逐項保存 checkpoint 與安全摘要。"""
    summary = {name: 0 for name in ("added", "updated", "skipped", "quarantined", "failed")}
    _upsert_company(connection, company)
    for filing in filings:
        is_new, current_status, raw_path = _upsert_filing(connection, filing)
        if current_status in {"downloaded", "parsed", "indexed"} and raw_path:
            if Path(raw_path).is_file():
                summary["skipped"] += 1
                continue
        checkpoint_item(
            connection,
            run_id,
            filing.accession_number,
            stage="discovered",
            status="running",
        )
        try:
            content = await client.fetch_filing_html(
                filing.cik,
                filing.accession_number,
                filing.primary_document,
            )
            _validate_filing_content_identity(content, filing)
            stored = raw_store.save(filing.cik, filing.accession_number, content)
        except (FilingIdentityError, RawContentConflict):
            _set_filing_error(
                connection,
                filing.accession_number,
                status="quarantined",
                error_code="FILING_IDENTITY_INVALID",
            )
            checkpoint_item(
                connection,
                run_id,
                filing.accession_number,
                stage="quarantined",
                status="failed",
                error_code="FILING_IDENTITY_INVALID",
            )
            summary["quarantined"] += 1
            continue
        except SecClientError as error:
            _set_filing_error(
                connection,
                filing.accession_number,
                status="failed",
                error_code=error.code,
            )
            checkpoint_item(
                connection,
                run_id,
                filing.accession_number,
                stage="failed",
                status="failed",
                error_code=error.code,
            )
            summary["failed"] += 1
            continue

        connection.execute(
            """
            UPDATE filings
            SET raw_path = %s,
                content_sha256 = %s,
                processing_status = 'downloaded',
                error_code = NULL,
                updated_at = now()
            WHERE accession_number = %s
            """,
            (str(stored.path), stored.sha256, filing.accession_number),
        )
        checkpoint_item(
            connection,
            run_id,
            filing.accession_number,
            stage="downloaded",
            status="completed",
        )
        summary["added" if is_new else "updated"] += 1
    return summary


async def ingest_company_facts(
    connection: psycopg.Connection[Any],
    client: SecClient,
    company: Company,
    *,
    allowed_accessions: set[str] | None = None,
) -> XbrlIngestionSummary:
    payload = await client.fetch_company_facts(company.cik)
    facts, duplicates, skipped, warnings = _normalize_company_facts(
        payload,
        company.cik,
    )
    if allowed_accessions is not None:
        outside_scope = [
            fact for fact in facts if fact.accession_number not in allowed_accessions
        ]
        facts = [
            fact for fact in facts if fact.accession_number in allowed_accessions
        ]
        skipped += len(outside_scope)
        if outside_scope:
            warnings.append(f"OUTSIDE_CORPUS_SCOPE:{len(outside_scope)}")
    inserted = 0
    updated = 0
    with connection.transaction():
        for fact in facts:
            exists = connection.execute(
                """
                SELECT 1
                FROM xbrl_facts
                WHERE cik = %s
                  AND taxonomy = %s
                  AND concept = %s
                  AND unit = %s
                  AND start_date IS NOT DISTINCT FROM %s
                  AND end_date = %s
                  AND form = %s
                  AND filed_date = %s
                  AND accession_number = %s
                """,
                fact.identity,
            ).fetchone()
            connection.execute(
                """
                INSERT INTO xbrl_facts (
                    cik,
                    accession_number,
                    taxonomy,
                    concept,
                    unit,
                    value,
                    start_date,
                    end_date,
                    fiscal_year,
                    fiscal_period,
                    form,
                    filed_date,
                    frame
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    cik,
                    taxonomy,
                    concept,
                    unit,
                    start_date,
                    end_date,
                    form,
                    filed_date,
                    accession_number
                ) DO UPDATE
                SET value = EXCLUDED.value,
                    fiscal_year = EXCLUDED.fiscal_year,
                    fiscal_period = EXCLUDED.fiscal_period,
                    frame = EXCLUDED.frame
                """,
                (
                    fact.cik,
                    fact.accession_number,
                    fact.taxonomy,
                    fact.concept,
                    fact.unit,
                    fact.value,
                    fact.start_date,
                    fact.end_date,
                    fact.fiscal_year,
                    fact.fiscal_period,
                    fact.form,
                    fact.filed_date,
                    fact.frame,
                ),
            )
            if exists:
                updated += 1
            else:
                inserted += 1
    return XbrlIngestionSummary(
        inserted=inserted,
        updated=updated,
        duplicates=duplicates,
        skipped=skipped,
        warnings=tuple(warnings),
    )


def _normalize_company_facts(
    payload: dict[str, Any],
    expected_cik: str,
) -> tuple[list[XbrlFact], int, int, list[str]]:
    facts: list[XbrlFact] = []
    seen: set[tuple[object, ...]] = set()
    warnings: list[str] = []
    duplicates = 0
    skipped = 0
    taxonomies = payload.get("facts", {})
    for taxonomy, concepts in taxonomies.items():
        if not isinstance(concepts, dict):
            raise SecClientError("SEC_SCHEMA_INVALID", "companyfacts concepts 無效")
        for concept, definition in concepts.items():
            if taxonomy != "us-gaap":
                skipped += 1
                warnings.append(f"CUSTOM_TAXONOMY_UNSUPPORTED:{taxonomy}:{concept}")
                continue
            if concept not in SUPPORTED_XBRL_CONCEPTS:
                skipped += 1
                warnings.append(f"CONCEPT_UNSUPPORTED:{taxonomy}:{concept}")
                continue
            units = definition.get("units") if isinstance(definition, dict) else None
            if not isinstance(units, dict):
                raise SecClientError("SEC_SCHEMA_INVALID", "companyfacts units 無效")
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    raise SecClientError("SEC_SCHEMA_INVALID", "companyfacts unit facts 無效")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise SecClientError("SEC_SCHEMA_INVALID", "companyfact 無效")
                    if not entry.get("accn"):
                        skipped += 1
                        warnings.append(f"MISSING_ACCESSION:{taxonomy}:{concept}")
                        continue
                    if any(
                        entry.get(field) in (None, "")
                        for field in ("val", "end", "fy", "fp", "form", "filed")
                    ):
                        skipped += 1
                        warnings.append(f"INCOMPLETE_FACT:{taxonomy}:{concept}")
                        continue
                    try:
                        fact = XbrlFact(
                            cik=normalize_cik(expected_cik),
                            accession_number=normalize_accession(str(entry["accn"])),
                            taxonomy=taxonomy,
                            concept=concept,
                            unit=str(unit),
                            value=Decimal(str(entry["val"])),
                            start_date=(
                                date.fromisoformat(entry["start"])
                                if entry.get("start")
                                else None
                            ),
                            end_date=date.fromisoformat(entry["end"]),
                            fiscal_year=int(entry["fy"]),
                            fiscal_period=str(entry["fp"]),
                            form=str(entry["form"]),
                            filed_date=date.fromisoformat(entry["filed"]),
                            frame=(str(entry["frame"]) if entry.get("frame") else None),
                        )
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                        InvalidOperation,
                    ) as error:
                        raise SecClientError(
                            "SEC_SCHEMA_INVALID", "companyfact 欄位無效"
                        ) from error
                    if fact.form not in ALLOWED_FORMS:
                        skipped += 1
                        warnings.append(f"FORM_UNSUPPORTED:{fact.form}")
                        continue
                    if fact.identity in seen:
                        duplicates += 1
                        continue
                    seen.add(fact.identity)
                    facts.append(fact)
    return facts, duplicates, skipped, warnings


def _upsert_company(connection: psycopg.Connection[Any], company: Company) -> None:
    connection.execute(
        """
        INSERT INTO companies (cik, ticker, company_name, exchange)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (cik) DO UPDATE
        SET ticker = EXCLUDED.ticker,
            company_name = EXCLUDED.company_name,
            exchange = EXCLUDED.exchange,
            updated_at = now()
        """,
        (company.cik, company.ticker, company.company_name, company.exchange),
    )


def _upsert_filing(
    connection: psycopg.Connection[Any],
    filing: FilingMetadata,
) -> tuple[bool, str, str | None]:
    existing = connection.execute(
        """
        SELECT processing_status, raw_path
        FROM filings
        WHERE accession_number = %s
        """,
        (filing.accession_number,),
    ).fetchone()
    if existing:
        connection.execute(
            """
            UPDATE filings
            SET cik = %s,
                form = %s,
                filed_date = %s,
                period_end = %s,
                primary_document = %s,
                source_url = %s,
                updated_at = now()
            WHERE accession_number = %s
            """,
            (
                filing.cik,
                filing.form,
                filing.filed_date,
                filing.period_end,
                filing.primary_document,
                filing.source_url,
                filing.accession_number,
            ),
        )
        return False, existing[0], existing[1]
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
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
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
    return True, "discovered", None


def _validate_filing_content_identity(content: bytes, filing: FilingMetadata) -> None:
    if filing.cik.encode("ascii") not in content:
        raise FilingIdentityError("HTML 未包含預期 CIK")


def _set_filing_error(
    connection: psycopg.Connection[Any],
    accession_number: str,
    *,
    status: str,
    error_code: str,
) -> None:
    connection.execute(
        """
        UPDATE filings
        SET processing_status = %s, error_code = %s, updated_at = now()
        WHERE accession_number = %s
        """,
        (status, error_code, accession_number),
    )


def build_intent_hash(scope: ResearchScope, pipeline_version: str) -> str:
    if not pipeline_version.strip():
        raise ValueError("pipeline_version 不得為空")
    intent = {"pipeline_version": pipeline_version, "scope": scope.as_dict()}
    canonical = json.dumps(
        intent,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def start_run(
    connection: psycopg.Connection[Any],
    scope: ResearchScope,
    pipeline_version: str,
) -> UUID:
    started_at = time.perf_counter()
    intent_hash = build_intent_hash(scope, pipeline_version)
    with connection.transaction():
        existing = connection.execute(
            """
            SELECT run_id, status
            FROM ingestion_runs
            WHERE intent_hash = %s
            FOR UPDATE
            """,
            (intent_hash,),
        ).fetchone()
        if existing and existing[1] == "running":
            _log_event(
                operation="start_run",
                status="conflict",
                request_id=str(existing[0]),
                duration_ms=_duration_ms(started_at),
                error_code="INGESTION_CONFLICT",
            )
            raise IngestionConflict("相同 corpus intent 已在執行")
        if existing:
            run_id = existing[0]
            connection.execute(
                """
                UPDATE ingestion_runs
                SET scope = %s,
                    status = 'running',
                    counts = '{}'::jsonb,
                    started_at = now(),
                    completed_at = NULL
                WHERE run_id = %s
                """,
                (Jsonb(scope.as_dict()), run_id),
            )
        else:
            run_id = connection.execute(
                """
                INSERT INTO ingestion_runs (scope, intent_hash, status)
                VALUES (%s, %s, 'running')
                RETURNING run_id
                """,
                (Jsonb(scope.as_dict()), intent_hash),
            ).fetchone()[0]
    _log_event(
        operation="start_run",
        status="ok",
        request_id=str(run_id),
        duration_ms=_duration_ms(started_at),
    )
    return run_id


def checkpoint_item(
    connection: psycopg.Connection[Any],
    run_id: UUID,
    accession_number: str,
    *,
    stage: str,
    status: str,
    error_code: str | None = None,
) -> None:
    started_at = time.perf_counter()
    accession = normalize_accession(accession_number)
    if stage not in ITEM_STAGES:
        raise ValueError("未知 ingestion stage")
    if status not in ITEM_STATUSES:
        raise ValueError("未知 ingestion item status")
    if error_code is not None and not ERROR_CODE_PATTERN.fullmatch(error_code):
        raise ValueError("error_code 格式無效")
    connection.execute(
        """
        INSERT INTO ingestion_items (
            run_id,
            accession_number,
            stage,
            status,
            error_code,
            attempt_count
        ) VALUES (%s, %s, %s, %s, %s, 1)
        ON CONFLICT (run_id, accession_number) DO UPDATE
        SET stage = EXCLUDED.stage,
            status = EXCLUDED.status,
            error_code = EXCLUDED.error_code,
            attempt_count = ingestion_items.attempt_count + 1,
            updated_at = now()
        """,
        (run_id, accession, stage, status, error_code),
    )
    _log_event(
        operation="checkpoint_item",
        status=status,
        request_id=str(run_id),
        duration_ms=_duration_ms(started_at),
        result_count=1,
        error_code=error_code,
    )


def finish_run(
    connection: psycopg.Connection[Any],
    run_id: UUID,
    *,
    status: str,
    extra_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    started_at = time.perf_counter()
    if status not in {"completed", "failed"}:
        raise ValueError("run 結束狀態只能是 completed 或 failed")
    rows = connection.execute(
        """
        SELECT stage, count(*)
        FROM ingestion_items
        WHERE run_id = %s
        GROUP BY stage
        ORDER BY stage
        """,
        (run_id,),
    ).fetchall()
    counts = {stage: count for stage, count in rows}
    item_count = sum(counts.values())
    counts.update(extra_counts or {})
    updated = connection.execute(
        """
        UPDATE ingestion_runs
        SET status = %s, counts = %s, completed_at = now()
        WHERE run_id = %s AND status = 'running'
        """,
        (status, Jsonb(counts), run_id),
    )
    if updated.rowcount != 1:
        raise ValueError("run 不存在或不在 running 狀態")
    _log_event(
        operation="finish_run",
        status=status,
        request_id=str(run_id),
        duration_ms=_duration_ms(started_at),
        result_count=item_count,
    )
    return counts


def _duration_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _log_event(
    *,
    operation: str,
    status: str,
    request_id: str,
    duration_ms: int,
    result_count: int = 0,
    error_code: str | None = None,
) -> None:
    logger.info(
        json.dumps(
            {
                "event": "ingestion_operation",
                "component": "ingestion",
                "operation": operation,
                "status": status,
                "duration_ms": duration_ms,
                "result_count": result_count,
                "error_code": error_code,
                "retry_count": 0,
                "request_id": request_id,
                "run_id": request_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
