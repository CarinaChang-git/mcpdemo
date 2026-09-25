"""MCP 工具共用 envelope、錯誤語意與 boundary 驗證。"""

import inspect
import base64
import binascii
from dataclasses import asdict
from datetime import date
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import psycopg
from mcp.server import MCPServer
from openai import OpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sec_research.config import Settings
from sec_research.rag import (
    ALLOWED_SECTIONS,
    EmbeddingBoundary,
    IndexUnavailableError,
    hybrid_search,
    resolve_citation,
    OpenAIEmbeddingBoundary,
)
from sec_research.ingest import SUPPORTED_XBRL_CONCEPTS
from sec_research.sec_client import (
    SecClient,
    SecClientError,
    build_filing_url,
    normalize_accession,
)


ToolStatus = Literal["ok", "partial", "not_found"]
ErrorCode = Literal[
    "INVALID_ARGUMENT",
    "DEPENDENCY_UNAVAILABLE",
    "RATE_LIMITED",
    "INDEX_UNAVAILABLE",
    "INTERNAL_ERROR",
]
InputModel = TypeVar("InputModel", bound=BaseModel)
ToolHandler = Callable[
    [InputModel],
    dict[str, Any] | Awaitable[dict[str, Any]],
]
logger = logging.getLogger(__name__)

Ticker = Literal["AAPL", "MSFT", "NVDA"]
CorpusForm = Literal["10-K", "10-Q"]
LatestForm = Literal["10-K", "10-Q", "8-K"]
SectionCode = Literal[
    "ITEM_1",
    "ITEM_1A",
    "ITEM_1C",
    "ITEM_7",
    "ITEM_8",
    "PART_I_ITEM_1",
    "PART_I_ITEM_2",
    "PART_II_ITEM_1A",
]
FinancialConcept = Literal[
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "ResearchAndDevelopmentExpense",
    "NetIncomeLoss",
    "Assets",
]

ERROR_CONTRACT: dict[str, tuple[str, bool, int | None]] = {
    "INVALID_ARGUMENT": ("輸入參數無效", False, None),
    "DEPENDENCY_UNAVAILABLE": ("必要依賴目前無法使用", True, None),
    "RATE_LIMITED": ("依賴服務目前限制請求速率", True, 5),
    "INDEX_UNAVAILABLE": ("檢索索引目前無法使用", True, None),
    "INTERNAL_ERROR": ("伺服器無法完成要求", False, None),
}


class ListFilingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tickers: list[Literal["AAPL", "MSFT", "NVDA"]] | None = Field(
        default=None, min_length=1, max_length=3
    )
    forms: list[Literal["10-K", "10-Q"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    filed_from: date | None = None
    filed_to: date | None = None
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2000)

    @field_validator("tickers", "forms", mode="before")
    @classmethod
    def normalize_choices(cls, value: object) -> object:
        if isinstance(value, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in value]
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "ListFilingsInput":
        if self.filed_from and self.filed_to and self.filed_from > self.filed_to:
            raise ValueError("filed_from 不得晚於 filed_to")
        return self


class GetCorpusStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetLatestFilingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: Literal["AAPL", "MSFT", "NVDA"]
    forms: list[Literal["10-K", "10-Q", "8-K"]] = Field(
        default_factory=lambda: ["10-K", "10-Q", "8-K"],
        min_length=1,
        max_length=3,
    )
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("forms", mode="before")
    @classmethod
    def normalize_forms(cls, value: object) -> object:
        if isinstance(value, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in value]
        return value


COMPANY_CIKS = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
}


class SearchFilingSectionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    tickers: list[Literal["AAPL", "MSFT", "NVDA"]] | None = Field(
        default=None, min_length=1, max_length=3
    )
    forms: list[Literal["10-K", "10-Q"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    filed_from: date | None = None
    filed_to: date | None = None
    sections: list[str] | None = Field(default=None, min_length=1, max_length=8)
    top_k: int = Field(default=8, ge=1, le=12)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query 不得為空")
        return normalized

    @field_validator("tickers", "forms", "sections", mode="before")
    @classmethod
    def normalize_lists(cls, value: object) -> object:
        if isinstance(value, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("sections")
    @classmethod
    def validate_sections(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not set(value) <= ALLOWED_SECTIONS:
            raise ValueError("sections 含有不支援的值")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "SearchFilingSectionsInput":
        if self.filed_from and self.filed_to and self.filed_from > self.filed_to:
            raise ValueError("filed_from 不得晚於 filed_to")
        return self


class ReadFilingSectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accession_number: str
    section_code: str
    cursor: str | None = Field(default=None, max_length=2000)
    max_chars: int = Field(default=8000, ge=1000, le=20_000)

    @field_validator("accession_number")
    @classmethod
    def validate_accession(cls, value: str) -> str:
        return normalize_accession(value)

    @field_validator("section_code")
    @classmethod
    def validate_section(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in ALLOWED_SECTIONS:
            raise ValueError("section_code 不支援")
        return normalized


class GetFinancialMetricInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: Literal["AAPL", "MSFT", "NVDA"]
    concepts: list[str] = Field(min_length=1, max_length=5)
    period_from: date | None = None
    period_to: date | None = None
    forms: list[Literal["10-K", "10-Q"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    unit: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("forms", mode="before")
    @classmethod
    def normalize_forms(cls, value: object) -> object:
        if isinstance(value, list):
            return [item.strip().upper() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("concepts")
    @classmethod
    def validate_concepts(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if len(normalized) != len(value) or not set(normalized) <= SUPPORTED_XBRL_CONCEPTS:
            raise ValueError("concepts 必須是支援且不重複的 taxonomy concepts")
        return normalized

    @model_validator(mode="after")
    def validate_period(self) -> "GetFinancialMetricInput":
        if self.period_from and self.period_to and self.period_from > self.period_to:
            raise ValueError("period_from 不得晚於 period_to")
        return self


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=1, max_length=500)


def create_mcp_server(
    database_url: str | SecretStr,
    sec_client: Any,
    embeddings: EmbeddingBoundary,
    *,
    statement_timeout_ms: int = 10_000,
) -> MCPServer:
    """建立六工具、三 resources 與 Streamable HTTP 健康端點。"""
    dsn = (
        database_url.get_secret_value()
        if isinstance(database_url, SecretStr)
        else database_url
    )
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise ValueError("database_url 必須使用 PostgreSQL URL")
    server = MCPServer(
        "sec-filing-research",
        description="只讀 SEC filing corpus research server",
        version="0.1.0",
    )

    @server.tool(name="list_filings", structured_output=True)
    async def list_filings_mcp(
        tickers: Annotated[list[Ticker], Field(min_length=1, max_length=3)]
        | None = None,
        forms: Annotated[list[CorpusForm], Field(min_length=1, max_length=2)]
        | None = None,
        filed_from: date | None = None,
        filed_to: date | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """列出本機 corpus 已知 filing。"""
        with psycopg.connect(dsn) as connection:
            return await list_filings_tool(
                connection,
                {
                    "tickers": tickers,
                    "forms": forms,
                    "filed_from": filed_from,
                    "filed_to": filed_to,
                    "limit": limit,
                    "cursor": cursor,
                },
                statement_timeout_ms=statement_timeout_ms,
            )

    @server.tool(name="get_corpus_status", structured_output=True)
    async def get_corpus_status_mcp() -> dict[str, Any]:
        """回傳本機 corpus 與 active index 狀態。"""
        with psycopg.connect(dsn) as connection:
            return await get_corpus_status_tool(
                connection,
                {},
                statement_timeout_ms=statement_timeout_ms,
            )

    @server.tool(name="get_latest_filings", structured_output=True)
    async def get_latest_filings_mcp(
        ticker: Ticker,
        forms: Annotated[list[LatestForm], Field(min_length=1, max_length=3)]
        | None = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> dict[str, Any]:
        """由 SEC submissions 即時讀取最新 filing metadata。"""
        payload: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if forms is not None:
            payload["forms"] = forms
        with psycopg.connect(dsn) as connection:
            return await get_latest_filings_tool(
                connection,
                sec_client,
                payload,
                statement_timeout_ms=statement_timeout_ms,
            )

    @server.tool(name="search_filing_sections", structured_output=True)
    async def search_filing_sections_mcp(
        query: Annotated[str, Field(min_length=1, max_length=2000)],
        tickers: Annotated[list[Ticker], Field(min_length=1, max_length=3)]
        | None = None,
        forms: Annotated[list[CorpusForm], Field(min_length=1, max_length=2)]
        | None = None,
        filed_from: date | None = None,
        filed_to: date | None = None,
        sections: Annotated[list[SectionCode], Field(min_length=1, max_length=8)]
        | None = None,
        top_k: Annotated[int, Field(ge=1, le=12)] = 8,
    ) -> dict[str, Any]:
        """對 active index 執行 metadata-first hybrid retrieval。"""
        with psycopg.connect(dsn) as connection:
            return await search_filing_sections_tool(
                connection,
                embeddings,
                {
                    "query": query,
                    "tickers": tickers,
                    "forms": forms,
                    "filed_from": filed_from,
                    "filed_to": filed_to,
                    "sections": sections,
                    "top_k": top_k,
                },
                statement_timeout_ms=statement_timeout_ms,
            )

    @server.tool(name="read_filing_section", structured_output=True)
    async def read_filing_section_mcp(
        accession_number: Annotated[
            str, Field(pattern=r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
        ],
        section_code: SectionCode,
        cursor: str | None = None,
        max_chars: Annotated[int, Field(ge=1000, le=20_000)] = 8000,
    ) -> dict[str, Any]:
        """依 accession 與 section 精確分頁讀取 filing 原文。"""
        with psycopg.connect(dsn) as connection:
            return await read_filing_section_tool(
                connection,
                {
                    "accession_number": accession_number,
                    "section_code": section_code,
                    "cursor": cursor,
                    "max_chars": max_chars,
                },
                statement_timeout_ms=statement_timeout_ms,
            )

    @server.tool(name="get_financial_metric", structured_output=True)
    async def get_financial_metric_mcp(
        ticker: Ticker,
        concepts: Annotated[
            list[FinancialConcept], Field(min_length=1, max_length=5)
        ],
        period_from: date | None = None,
        period_to: date | None = None,
        forms: Annotated[list[CorpusForm], Field(min_length=1, max_length=2)]
        | None = None,
        unit: str | None = None,
    ) -> dict[str, Any]:
        """直接讀取本機 XBRL facts，不使用向量檢索。"""
        with psycopg.connect(dsn) as connection:
            return await get_financial_metric_tool(
                connection,
                {
                    "ticker": ticker,
                    "concepts": concepts,
                    "period_from": period_from,
                    "period_to": period_to,
                    "forms": forms,
                    "unit": unit,
                },
                statement_timeout_ms=statement_timeout_ms,
            )

    @server.resource(
        "sec://corpus/status",
        name="corpus_status",
        mime_type="application/json",
    )
    async def corpus_status_resource() -> str:
        """本機 corpus 與 active index 狀態。"""
        with psycopg.connect(dsn) as connection:
            response = await read_resource(
                connection,
                "sec://corpus/status",
                statement_timeout_ms=statement_timeout_ms,
            )
        return json.dumps(response, ensure_ascii=False)

    @server.resource(
        "sec://filings/{accession_number}",
        name="filing",
        mime_type="application/json",
    )
    async def filing_resource(accession_number: str) -> str:
        """本機 filing metadata 與章節清單。"""
        with psycopg.connect(dsn) as connection:
            response = await read_resource(
                connection,
                f"sec://filings/{accession_number}",
                statement_timeout_ms=statement_timeout_ms,
            )
        return json.dumps(response, ensure_ascii=False)

    @server.resource(
        "sec://filings/{accession_number}/sections/{section_code}",
        name="filing_section",
        mime_type="application/json",
    )
    async def filing_section_resource(
        accession_number: str,
        section_code: str,
    ) -> str:
        """本機 filing 的精確 section 內容。"""
        with psycopg.connect(dsn) as connection:
            response = await read_resource(
                connection,
                f"sec://filings/{accession_number}/sections/{section_code}",
                statement_timeout_ms=statement_timeout_ms,
            )
        return json.dumps(response, ensure_ascii=False)

    @server.custom_route("/health/live", methods=["GET"])
    async def health_live(_: Request) -> Response:
        return JSONResponse({"status": "live"})

    @server.custom_route("/health/ready", methods=["GET"])
    async def health_ready(_: Request) -> Response:
        ready, code = _readiness(dsn, statement_timeout_ms)
        return JSONResponse(
            {"status": "ready"} if ready else {"status": "not_ready", "code": code},
            status_code=200 if ready else 503,
        )

    return server


def make_envelope(
    *,
    data: Any,
    status: ToolStatus = "ok",
    citations: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    page: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": status,
        "request_id": _request_id(request_id),
        "data": data,
        "citations": list(citations or []),
        "warnings": list(warnings or []),
    }
    if page is not None:
        response["page"] = page
    return response


def make_error(
    code: ErrorCode,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    message, retryable, retry_after = ERROR_CONTRACT[code]
    detail: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if retry_after is not None:
        detail["retry_after_seconds"] = retry_after
    return {
        "status": "error",
        "request_id": _request_id(request_id),
        "error": detail,
    }


async def list_filings_tool(
    connection: psycopg.Connection[Any],
    payload: object,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    return await execute_tool(
        ListFilingsInput,
        payload,
        lambda inputs: list_filings(
            connection,
            inputs,
            statement_timeout_ms=statement_timeout_ms,
            request_id=request_id,
        ),
        request_id=request_id,
    )


def list_filings(
    connection: psycopg.Connection[Any],
    inputs: ListFilingsInput,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    scope_hash = _catalog_scope_hash(inputs)
    offset = _decode_page_cursor(inputs.cursor, scope_hash) if inputs.cursor else 0
    with connection.transaction():
        _set_read_only_role(connection, statement_timeout_ms)
        rows = connection.execute(
            """
            SELECT
                filings.accession_number,
                companies.cik,
                companies.ticker,
                companies.company_name,
                filings.form,
                filings.filed_date,
                filings.period_end,
                filings.processing_status,
                filings.source_url
            FROM filings
            JOIN companies USING (cik)
            WHERE (%s OR companies.ticker = ANY(%s::text[]))
              AND (%s OR filings.form = ANY(%s::text[]))
              AND filings.filed_date >= COALESCE(%s::date, filings.filed_date)
              AND filings.filed_date <= COALESCE(%s::date, filings.filed_date)
            ORDER BY filings.filed_date DESC, filings.accession_number DESC
            LIMIT %s OFFSET %s
            """,
            (
                not inputs.tickers,
                list(inputs.tickers or []),
                not inputs.forms,
                list(inputs.forms or []),
                inputs.filed_from,
                inputs.filed_to,
                inputs.limit + 1,
                offset,
            ),
        ).fetchall()
    page_rows = rows[: inputs.limit]
    filings = [
        {
            "accession_number": row[0],
            "cik": row[1],
            "ticker": row[2],
            "company_name": row[3],
            "form": row[4],
            "filed_date": row[5].isoformat(),
            "period_end": row[6].isoformat(),
            "processing_status": row[7],
            "source_url": row[8],
        }
        for row in page_rows
    ]
    next_cursor = (
        _encode_page_cursor(scope_hash, offset + inputs.limit)
        if len(rows) > inputs.limit
        else None
    )
    return make_envelope(
        status="ok" if filings else "not_found",
        data={"filings": filings},
        page={"next_cursor": next_cursor, "limit": inputs.limit},
        request_id=request_id,
    )


async def get_corpus_status_tool(
    connection: psycopg.Connection[Any],
    payload: object,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    return await execute_tool(
        GetCorpusStatusInput,
        payload,
        lambda inputs: get_corpus_status(
            connection,
            inputs,
            statement_timeout_ms=statement_timeout_ms,
            request_id=request_id,
        ),
        request_id=request_id,
    )


def get_corpus_status(
    connection: psycopg.Connection[Any],
    _: GetCorpusStatusInput,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    with connection.transaction():
        _set_read_only_role(connection, statement_timeout_ms)
        summary = connection.execute(
            """
            SELECT
                ARRAY(
                    SELECT DISTINCT companies.ticker
                    FROM filings JOIN companies USING (cik)
                    ORDER BY companies.ticker
                ),
                ARRAY(SELECT DISTINCT form FROM filings ORDER BY form),
                min(period_end),
                max(period_end),
                count(*),
                count(*) FILTER (WHERE processing_status = 'failed'),
                count(*) FILTER (WHERE processing_status = 'quarantined'),
                max(updated_at),
                (SELECT count(*) FROM filing_sections),
                (SELECT count(*) FROM chunks),
                (SELECT count(*) FROM xbrl_facts)
            FROM filings
            """
        ).fetchone()
        active = connection.execute(
            """
            SELECT index_build_id, embedding_provider, embedding_model,
                   embedding_dimension, chunker_version, created_at
            FROM index_builds
            WHERE is_active AND status = 'ready'
            """
        ).fetchone()
    active_build = (
        {
            "index_build_id": str(active[0]),
            "embedding_provider": active[1],
            "embedding_model": active[2],
            "embedding_dimension": active[3],
            "chunker_version": active[4],
            "created_at": active[5].isoformat(),
        }
        if active
        else None
    )
    return make_envelope(
        data={
            "scope": {
                "tickers": summary[0],
                "forms": summary[1],
                "period_from": summary[2].isoformat() if summary[2] else None,
                "period_to": summary[3].isoformat() if summary[3] else None,
            },
            "counts": {
                "filings": summary[4],
                "failed": summary[5],
                "quarantined": summary[6],
                "sections": summary[8],
                "chunks": summary[9],
                "facts": summary[10],
            },
            "active_index_build": active_build,
            "last_sync_at": summary[7].isoformat() if summary[7] else None,
        },
        request_id=request_id,
    )


async def get_latest_filings_tool(
    connection: psycopg.Connection[Any],
    client: SecClient,
    payload: object,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    async def handler(inputs: GetLatestFilingsInput) -> dict[str, Any]:
        submissions = await client.fetch_submissions(COMPANY_CIKS[inputs.ticker])
        return get_latest_filings(
            connection,
            submissions,
            inputs,
            statement_timeout_ms=statement_timeout_ms,
            request_id=request_id,
        )

    return await execute_tool(
        GetLatestFilingsInput,
        payload,
        handler,
        request_id=request_id,
    )


def get_latest_filings(
    connection: psycopg.Connection[Any],
    submissions: dict[str, Any],
    inputs: GetLatestFilingsInput,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
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
        raise SecClientError("SEC_SCHEMA_INVALID", "submissions schema 無效") from error
    if any(not isinstance(values, list) for values in columns.values()):
        raise SecClientError("SEC_SCHEMA_INVALID", "submissions 欄位必須是陣列")
    lengths = {len(values) for values in columns.values()}
    if len(lengths) != 1:
        raise SecClientError("SEC_SCHEMA_INVALID", "submissions 欄位長度不一致")
    records = [
        dict(zip(columns, values, strict=True))
        for values in zip(*columns.values(), strict=True)
    ]
    selected = [record for record in records if record["form"] in inputs.forms][
        : inputs.limit
    ]
    accessions = [str(record["accessionNumber"]) for record in selected]
    with connection.transaction():
        _set_read_only_role(connection, statement_timeout_ms)
        local_accessions = {
            row[0]
            for row in connection.execute(
                """
                SELECT accession_number
                FROM filings
                WHERE accession_number = ANY(%s::text[])
                """,
                (accessions,),
            )
        }
    retrieved_at = datetime.now(UTC).isoformat()
    filings = [
        {
            "ticker": inputs.ticker,
            "cik": COMPANY_CIKS[inputs.ticker],
            "company_name": str(submissions["name"]),
            "form": str(record["form"]),
            "filing_date": date.fromisoformat(str(record["filingDate"])).isoformat(),
            "period_end": date.fromisoformat(str(record["reportDate"])).isoformat(),
            "accession_number": str(record["accessionNumber"]),
            "primary_document": str(record["primaryDocument"]),
            "source_url": build_filing_url(
                COMPANY_CIKS[inputs.ticker],
                str(record["accessionNumber"]),
                str(record["primaryDocument"]),
            ),
            "in_local_corpus": str(record["accessionNumber"]) in local_accessions,
            "retrieved_at": retrieved_at,
        }
        for record in selected
    ]
    return make_envelope(
        status="ok" if filings else "not_found",
        data={"filings": filings},
        request_id=request_id,
    )


async def search_filing_sections_tool(
    connection: psycopg.Connection[Any],
    embeddings: EmbeddingBoundary,
    payload: object,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    return await execute_tool(
        SearchFilingSectionsInput,
        payload,
        lambda inputs: search_filing_sections(
            connection,
            embeddings,
            inputs,
            statement_timeout_ms=statement_timeout_ms,
            request_id=request_id,
        ),
        request_id=request_id,
    )


def search_filing_sections(
    connection: psycopg.Connection[Any],
    embeddings: EmbeddingBoundary,
    inputs: SearchFilingSectionsInput,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    with connection.transaction():
        _set_read_only_role(connection, statement_timeout_ms)
        page = hybrid_search(
            connection,
            embeddings,
            inputs.query,
            tickers=inputs.tickers,
            forms=inputs.forms,
            filed_from=inputs.filed_from,
            filed_to=inputs.filed_to,
            sections=inputs.sections,
            top_k=inputs.top_k,
        )
    matches = []
    citations = []
    for result in page.results:
        match = asdict(result)
        match["chunk_id"] = str(result.chunk_id)
        match["index_build_id"] = str(result.index_build_id)
        match["filing_date"] = result.filing_date.isoformat()
        match["period_end"] = result.period_end.isoformat()
        match["previous_chunk_id"] = (
            str(result.previous_chunk_id) if result.previous_chunk_id else None
        )
        match["next_chunk_id"] = (
            str(result.next_chunk_id) if result.next_chunk_id else None
        )
        citation = asdict(resolve_citation(result))
        citation["filing_date"] = result.filing_date.isoformat()
        citation["period_end"] = result.period_end.isoformat()
        citation["excerpt"] = result.content_text[:500]
        matches.append(match)
        citations.append(citation)
    return make_envelope(
        status="ok" if matches else "not_found",
        data={
            "active_index_build_id": str(page.active_index_build_id),
            "matches": matches,
        },
        citations=citations,
        request_id=request_id,
    )


async def read_filing_section_tool(
    connection: psycopg.Connection[Any],
    payload: object,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    return await execute_tool(
        ReadFilingSectionInput,
        payload,
        lambda inputs: read_filing_section(
            connection,
            inputs,
            statement_timeout_ms=statement_timeout_ms,
            request_id=request_id,
        ),
        request_id=request_id,
    )


def read_filing_section(
    connection: psycopg.Connection[Any],
    inputs: ReadFilingSectionInput,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    scope_hash = hashlib.sha256(
        f"{inputs.accession_number}:{inputs.section_code}:{inputs.max_chars}".encode()
    ).hexdigest()
    offset = _decode_page_cursor(inputs.cursor, scope_hash) if inputs.cursor else 0
    with connection.transaction():
        _set_read_only_role(connection, statement_timeout_ms)
        row = connection.execute(
            """
            SELECT
                companies.company_name,
                companies.cik,
                companies.ticker,
                filings.form,
                filings.filed_date,
                filings.period_end,
                filings.accession_number,
                sections.section_code,
                sections.section_title,
                sections.content_text,
                filings.source_url
            FROM filing_sections AS sections
            JOIN filings USING (accession_number)
            JOIN companies USING (cik)
            WHERE sections.accession_number = %s
              AND sections.section_code = %s
              AND sections.parse_status = 'parsed'
            ORDER BY sections.ordinal
            LIMIT 1
            """,
            (inputs.accession_number, inputs.section_code),
        ).fetchone()
    if row is None:
        return make_envelope(
            status="not_found",
            data={"content_text": ""},
            citations=[],
            page={"next_cursor": None, "max_chars": inputs.max_chars},
            request_id=request_id,
        )
    content = row[9]
    if offset > len(content):
        raise ValueError("cursor 超出 section 範圍")
    excerpt = content[offset : offset + inputs.max_chars]
    next_offset = offset + len(excerpt)
    next_cursor = (
        _encode_page_cursor(scope_hash, next_offset)
        if next_offset < len(content)
        else None
    )
    citation_id = hashlib.sha256(
        f"{row[6]}:{row[7]}".encode("utf-8")
    ).hexdigest()[:24]
    citation = {
        "citation_id": citation_id,
        "company_name": row[0],
        "ticker": row[2],
        "form": row[3],
        "period_end": row[5].isoformat(),
        "filing_date": row[4].isoformat(),
        "accession_number": row[6],
        "section_code": row[7],
        "source_url": row[10],
    }
    return make_envelope(
        data={
            "company_name": row[0],
            "cik": row[1],
            "ticker": row[2],
            "form": row[3],
            "filing_date": row[4].isoformat(),
            "period_end": row[5].isoformat(),
            "accession_number": row[6],
            "section_code": row[7],
            "section_title": row[8],
            "content_text": excerpt,
            "source_url": row[10],
        },
        citations=[citation],
        page={"next_cursor": next_cursor, "max_chars": inputs.max_chars},
        request_id=request_id,
    )


async def get_financial_metric_tool(
    connection: psycopg.Connection[Any],
    payload: object,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    return await execute_tool(
        GetFinancialMetricInput,
        payload,
        lambda inputs: get_financial_metric(
            connection,
            inputs,
            statement_timeout_ms=statement_timeout_ms,
            request_id=request_id,
        ),
        request_id=request_id,
    )


def get_financial_metric(
    connection: psycopg.Connection[Any],
    inputs: GetFinancialMetricInput,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    with connection.transaction():
        _set_read_only_role(connection, statement_timeout_ms)
        rows = connection.execute(
            """
            SELECT
                facts.taxonomy,
                facts.concept,
                facts.value,
                facts.unit,
                facts.start_date,
                facts.end_date,
                facts.fiscal_year,
                facts.fiscal_period,
                facts.form,
                facts.filed_date,
                facts.accession_number,
                filings.source_url
            FROM xbrl_facts AS facts
            JOIN companies USING (cik)
            JOIN filings USING (accession_number)
            WHERE companies.ticker = %s
              AND facts.concept = ANY(%s::text[])
              AND facts.end_date >= COALESCE(%s::date, facts.end_date)
              AND facts.end_date <= COALESCE(%s::date, facts.end_date)
              AND (%s OR facts.form = ANY(%s::text[]))
              AND (%s::text IS NULL OR facts.unit = %s)
            ORDER BY facts.end_date DESC, facts.concept, facts.unit,
                     facts.taxonomy, facts.accession_number
            """,
            (
                inputs.ticker,
                inputs.concepts,
                inputs.period_from,
                inputs.period_to,
                not inputs.forms,
                list(inputs.forms or []),
                inputs.unit,
                inputs.unit,
            ),
        ).fetchall()
    facts = [
        {
            "taxonomy": row[0],
            "concept": row[1],
            "value": str(row[2]),
            "unit": row[3],
            "start_date": row[4].isoformat() if row[4] else None,
            "end_date": row[5].isoformat(),
            "fiscal_year": row[6],
            "fiscal_period": row[7],
            "form": row[8],
            "filed_date": row[9].isoformat(),
            "accession_number": row[10],
            "source_url": row[11],
        }
        for row in rows
    ]
    warnings: list[str] = []
    if inputs.unit is None:
        concept_units: dict[str, set[str]] = {}
        for fact in facts:
            concept_units.setdefault(fact["concept"], set()).add(fact["unit"])
        warnings.extend(
            f"MULTIPLE_UNITS:{concept}"
            for concept, units in concept_units.items()
            if len(units) > 1
        )
    warnings.extend(
        sorted(
            {
                f"CUSTOM_TAXONOMY:{fact['taxonomy']}:{fact['concept']}"
                for fact in facts
                if fact["taxonomy"] != "us-gaap"
            }
        )
    )
    return make_envelope(
        status=("not_found" if not facts else "partial" if warnings else "ok"),
        data={"facts": facts},
        warnings=warnings,
        request_id=request_id,
    )


async def read_resource(
    connection: psycopg.Connection[Any],
    uri: str,
    *,
    statement_timeout_ms: int = 10_000,
    request_id: str | None = None,
) -> dict[str, Any]:
    async def handler(inputs: ResourceInput) -> dict[str, Any]:
        parsed = urlsplit(inputs.uri)
        if parsed.scheme != "sec":
            raise ValueError("resource 只允許 sec:// URI")
        if parsed.netloc == "corpus" and parsed.path == "/status":
            return get_corpus_status(
                connection,
                GetCorpusStatusInput(),
                statement_timeout_ms=statement_timeout_ms,
                request_id=request_id,
            )
        if parsed.netloc != "filings" or parsed.query or parsed.fragment:
            raise ValueError("resource URI 不受支援")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 1:
            return _read_filing_resource(
                connection,
                normalize_accession(parts[0]),
                statement_timeout_ms=statement_timeout_ms,
                request_id=request_id,
            )
        if len(parts) == 3 and parts[1] == "sections":
            return read_filing_section(
                connection,
                ReadFilingSectionInput(
                    accession_number=parts[0],
                    section_code=parts[2],
                    max_chars=20_000,
                ),
                statement_timeout_ms=statement_timeout_ms,
                request_id=request_id,
            )
        raise ValueError("resource URI 不受支援")

    return await execute_tool(
        ResourceInput,
        {"uri": uri},
        handler,
        request_id=request_id,
    )


def _read_filing_resource(
    connection: psycopg.Connection[Any],
    accession_number: str,
    *,
    statement_timeout_ms: int,
    request_id: str | None,
) -> dict[str, Any]:
    with connection.transaction():
        _set_read_only_role(connection, statement_timeout_ms)
        filing = connection.execute(
            """
            SELECT companies.company_name, companies.cik, companies.ticker,
                   filings.form, filings.filed_date, filings.period_end,
                   filings.accession_number, filings.processing_status,
                   filings.source_url
            FROM filings JOIN companies USING (cik)
            WHERE filings.accession_number = %s
            """,
            (accession_number,),
        ).fetchone()
        sections = connection.execute(
            """
            SELECT section_code, section_title, ordinal, parse_status
            FROM filing_sections
            WHERE accession_number = %s
            ORDER BY ordinal
            """,
            (accession_number,),
        ).fetchall()
    if filing is None:
        return make_envelope(
            status="not_found", data={}, request_id=request_id
        )
    data = {
        "company_name": filing[0],
        "cik": filing[1],
        "ticker": filing[2],
        "form": filing[3],
        "filed_date": filing[4].isoformat(),
        "period_end": filing[5].isoformat(),
        "accession_number": filing[6],
        "processing_status": filing[7],
        "source_url": filing[8],
        "sections": [
            {
                "section_code": section[0],
                "section_title": section[1],
                "ordinal": section[2],
                "parse_status": section[3],
            }
            for section in sections
        ],
    }
    return make_envelope(data=data, request_id=request_id)


async def execute_tool(
    input_model: type[InputModel],
    payload: object,
    handler: ToolHandler[InputModel],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """驗證 MCP input，並將 domain 例外映射成固定安全錯誤。"""
    current_request_id = _request_id(request_id)
    started_at = time.perf_counter()
    exception_type: str | None = None
    try:
        validated = input_model.model_validate(payload)
    except ValidationError:
        result = make_error("INVALID_ARGUMENT", request_id=current_request_id)
    else:
        try:
            response = handler(validated)
            if inspect.isawaitable(response):
                response = await response
            if response.get("status") in {"ok", "partial", "not_found", "error"}:
                response["request_id"] = current_request_id
                result = response
            else:
                result = make_envelope(data=response, request_id=current_request_id)
        except (ValidationError, ValueError):
            result = make_error("INVALID_ARGUMENT", request_id=current_request_id)
        except IndexUnavailableError:
            result = make_error("INDEX_UNAVAILABLE", request_id=current_request_id)
        except SecClientError as error:
            if error.code == "SEC_RATE_LIMITED":
                code: ErrorCode = "RATE_LIMITED"
            elif error.retryable:
                code = "DEPENDENCY_UNAVAILABLE"
            else:
                code = "INTERNAL_ERROR"
            result = make_error(code, request_id=current_request_id)
        except Exception as error:
            exception_type = type(error).__name__
            result = make_error("INTERNAL_ERROR", request_id=current_request_id)
    data = result.get("data")
    result_count = 0
    if isinstance(data, dict):
        result_count = next(
            (
                len(data[key])
                for key in ("matches", "facts", "filings")
                if isinstance(data.get(key), list)
            ),
            int(bool(data)),
        )
    error = result.get("error")
    logger.info(
        json.dumps(
            {
                "component": "mcp",
                "operation": input_model.__name__,
                "status": result["status"],
                "duration_ms": round((time.perf_counter() - started_at) * 1000),
                "result_count": result_count,
                "error_code": error.get("code") if isinstance(error, dict) else None,
                "exception_type": exception_type,
                "retry_count": 0,
                "request_id": current_request_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return result


def _request_id(value: str | None) -> str:
    return str(UUID(value)) if value is not None else str(uuid4())


def _set_read_only_role(
    connection: psycopg.Connection[Any],
    statement_timeout_ms: int,
) -> None:
    if not 100 <= statement_timeout_ms <= 60_000:
        raise ValueError("statement timeout 必須介於 100 與 60000 毫秒")
    connection.execute("SET LOCAL ROLE sec_query")
    connection.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (str(statement_timeout_ms),),
    )


def _catalog_scope_hash(inputs: ListFilingsInput) -> str:
    encoded = json.dumps(
        {
            "tickers": inputs.tickers,
            "forms": inputs.forms,
            "filed_from": inputs.filed_from.isoformat() if inputs.filed_from else None,
            "filed_to": inputs.filed_to.isoformat() if inputs.filed_to else None,
            "limit": inputs.limit,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_page_cursor(scope_hash: str, offset: int) -> str:
    payload = json.dumps(
        {"scope": scope_hash, "offset": offset},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = json.dumps(
        {
            "payload": base64.urlsafe_b64encode(payload).decode("ascii"),
            "checksum": hashlib.sha256(payload).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(envelope).rstrip(b"=").decode("ascii")


def _decode_page_cursor(cursor: str, scope_hash: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        envelope = json.loads(raw)
        payload = base64.urlsafe_b64decode(envelope["payload"])
        decoded = json.loads(payload)
        if not hmac.compare_digest(
            envelope["checksum"], hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError
        offset = decoded["offset"]
        if decoded["scope"] != scope_hash or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("cursor 無效或不屬於目前查詢") from error


def _readiness(database_url: str, statement_timeout_ms: int) -> tuple[bool, str | None]:
    required_tables = (
        "companies",
        "filings",
        "filing_sections",
        "chunks",
        "index_builds",
        "chunk_embeddings",
        "xbrl_facts",
    )
    try:
        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _set_read_only_role(connection, statement_timeout_ms)
                tables = connection.execute(
                    "SELECT to_regclass('public.' || table_name) FROM unnest(%s::text[]) table_name",
                    (list(required_tables),),
                ).fetchall()
                if any(row[0] is None for row in tables):
                    return False, "SCHEMA_UNAVAILABLE"
                active = connection.execute(
                    """
                    SELECT 1
                    FROM index_builds
                    WHERE is_active AND status = 'ready'
                    """
                ).fetchone()
                if active is None:
                    return False, "INDEX_UNAVAILABLE"
    except (psycopg.Error, ValueError):
        return False, "DEPENDENCY_UNAVAILABLE"
    return True, None


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("sec_research").setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    settings = Settings()
    if settings.openrouter_api_key is None or not settings.openrouter_api_key.get_secret_value():
        raise ValueError("OPENROUTER_API_KEY 為 MCP 檢索的必要設定")
    embedding_client = OpenAI(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url="https://openrouter.ai/api/v1",
    )
    sec_client = SecClient(
        settings.sec_user_agent,
        requests_per_second=settings.sec_requests_per_second,
        max_retries=3,
        max_json_bytes=5_000_000,
        max_html_bytes=25_000_000,
    )
    server = create_mcp_server(
        settings.database_url,
        sec_client,
        OpenAIEmbeddingBoundary(embedding_client),
        statement_timeout_ms=settings.database_statement_timeout_ms,
    )
    server.run(
        "streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
