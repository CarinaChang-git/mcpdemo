"""章節切塊、穩定識別與 PostgreSQL 持久化。"""

from collections.abc import Sequence
import base64
import binascii
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import hmac
import json
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from openai import OpenAI

from sec_research.ingest import FilingMetadata
from sec_research.parser import ParsedSection
from sec_research.sec_client import SecClientError, validate_sec_url


EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSION = 1536
CHUNKER_VERSION = "v1"
ALLOWED_TICKERS = frozenset({"AAPL", "MSFT", "NVDA"})
ALLOWED_FORMS = frozenset({"10-K", "10-Q"})
ALLOWED_SECTIONS = frozenset(
    {
        "ITEM_1",
        "ITEM_1A",
        "ITEM_1C",
        "ITEM_7",
        "ITEM_8",
        "PART_I_ITEM_1",
        "PART_I_ITEM_2",
        "PART_II_ITEM_1A",
    }
)


class EmbeddingBoundary(Protocol):
    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]: ...


class OpenAIEmbeddingBoundary:
    """將 OpenAI SDK 回應縮成索引流程所需的向量清單。"""

    def __init__(self, client: OpenAI) -> None:
        self.client = client

    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        response = self.client.embeddings.create(
            input=list(texts),
            model=model,
            encoding_format="float",
        )
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


class IndexBuildError(RuntimeError):
    """候選索引無法完整建置。"""


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    index_build_id: UUID
    vector_count: int
    embedding_model: str = EMBEDDING_MODEL
    embedding_dimension: int = EMBEDDING_DIMENSION
    chunker_version: str = CHUNKER_VERSION


class IndexUnavailableError(RuntimeError):
    """目前沒有可供查詢的 active index。"""


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk_id: UUID
    index_build_id: UUID
    company_name: str
    cik: str
    ticker: str
    form: str
    filing_date: date
    period_end: date
    accession_number: str
    section_code: str
    section_title: str
    chunk_index: int
    content_text: str
    content_sha256: str
    source_url: str
    previous_chunk_id: UUID | None
    next_chunk_id: UUID | None
    keyword_rank: int | None
    vector_rank: int | None
    fusion_rank: int


@dataclass(frozen=True, slots=True)
class SearchPage:
    results: tuple[SearchResult, ...]
    active_index_build_id: UUID
    next_cursor: str | None


class CitationError(ValueError):
    """檢索結果無法解析成 SEC 官方引用。"""


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    company_name: str
    ticker: str
    form: str
    period_end: date
    filing_date: date
    accession_number: str
    section_code: str
    source_url: str


@dataclass(frozen=True, slots=True)
class RetrievalEvaluation:
    case_count: int
    metadata_accuracy: float
    recall_at_8: float
    citation_url_accuracy: float
    failures: tuple[str, ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: UUID
    accession_number: str
    company_name: str
    cik: str
    ticker: str
    form: str
    filing_date: date
    period_end: date
    section_code: str
    section_title: str
    chunk_index: int
    content_text: str
    token_count: int
    content_sha256: str
    source_url: str
    previous_chunk_id: UUID | None = None
    next_chunk_id: UUID | None = None


def chunk_section(
    filing: FilingMetadata,
    section: ParsedSection,
    *,
    min_tokens: int = 600,
    max_tokens: int = 900,
    overlap_tokens: int = 100,
) -> tuple[Chunk, ...]:
    if not 0 <= overlap_tokens < min_tokens <= max_tokens:
        raise ValueError("chunk token 限制無效")
    prefix = (
        f"{filing.company_name} | {filing.form} | period {filing.period_end.isoformat()} | "
        f"filed {filing.filed_date.isoformat()} | {section.code}"
    )
    prefix_count = len(prefix.split())
    body_min = max(1, min_tokens - prefix_count)
    body_max = max(1, max_tokens - prefix_count)
    if overlap_tokens >= body_max:
        raise ValueError("overlap 必須小於可用 chunk body")

    units = [line.strip() for line in section.content.splitlines() if line.strip()]
    tokens: list[str] = []
    boundaries: list[int] = []
    for unit in units:
        tokens.extend(unit.split())
        boundaries.append(len(tokens))
    if not tokens:
        raise ValueError("section content 不得為空")

    chunks: list[Chunk] = []
    start = 0
    while start < len(tokens):
        hard_end = min(start + body_max, len(tokens))
        if hard_end == len(tokens):
            end = hard_end
        else:
            preferred = [
                boundary
                for boundary in boundaries
                if start + body_min <= boundary <= hard_end
            ]
            end = max(preferred) if preferred else hard_end
        body = " ".join(tokens[start:end])
        content_text = f"{prefix}\n{body}"
        digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
        index = len(chunks)
        chunk_id = uuid5(
            NAMESPACE_URL,
            f"{filing.source_url}#{section.code}#{index}#{digest}",
        )
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                accession_number=filing.accession_number,
                company_name=filing.company_name,
                cik=filing.cik,
                ticker=filing.ticker,
                form=filing.form,
                filing_date=filing.filed_date,
                period_end=filing.period_end,
                section_code=section.code,
                section_title=section.title,
                chunk_index=index,
                content_text=content_text,
                token_count=len(content_text.split()),
                content_sha256=digest,
                source_url=filing.source_url,
            )
        )
        if end == len(tokens):
            break
        start = max(start + 1, end - overlap_tokens)

    return tuple(
        replace(
            chunk,
            previous_chunk_id=(chunks[index - 1].chunk_id if index else None),
            next_chunk_id=(
                chunks[index + 1].chunk_id if index + 1 < len(chunks) else None
            ),
        )
        for index, chunk in enumerate(chunks)
    )


def build_index(
    connection: psycopg.Connection[Any],
    embeddings: EmbeddingBoundary,
    *,
    index_build_id: UUID | None = None,
    batch_size: int = 100,
) -> IndexBuildResult:
    """建立完整候選索引，成功後才原子切換 active build。"""
    if batch_size < 1:
        raise ValueError("batch_size 必須大於 0")
    build_id = index_build_id or uuid4()
    with connection.transaction():
        row = connection.execute(
            """
            INSERT INTO index_builds (
                index_build_id,
                embedding_provider,
                embedding_model,
                embedding_dimension,
                chunker_version,
                status,
                is_active
            ) VALUES (%s, 'openrouter', %s, %s, %s, 'building', false)
            ON CONFLICT (index_build_id) DO UPDATE
            SET status = CASE
                    WHEN index_builds.status = 'ready' THEN 'ready'
                    ELSE 'building'
                END
            WHERE index_builds.embedding_provider = 'openrouter'
              AND index_builds.embedding_model = EXCLUDED.embedding_model
              AND index_builds.embedding_dimension = EXCLUDED.embedding_dimension
              AND index_builds.chunker_version = EXCLUDED.chunker_version
            RETURNING index_build_id
            """,
            (build_id, EMBEDDING_MODEL, EMBEDDING_DIMENSION, CHUNKER_VERSION),
        ).fetchone()
        if row is None:
            raise IndexBuildError("index build 版本設定不一致")

    try:
        missing = connection.execute(
            """
            SELECT chunks.chunk_id, chunks.content_text, chunks.content_sha256
            FROM chunks
            LEFT JOIN chunk_embeddings
              ON chunk_embeddings.chunk_id = chunks.chunk_id
             AND chunk_embeddings.index_build_id = %s
             AND chunk_embeddings.chunk_text_sha256 = chunks.content_sha256
            WHERE chunk_embeddings.chunk_id IS NULL
            ORDER BY chunks.chunk_id
            """,
            (build_id,),
        ).fetchall()
        connection.commit()
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            try:
                vectors = embeddings.embed(
                    [row[1] for row in batch],
                    model=EMBEDDING_MODEL,
                )
            except Exception as error:
                raise IndexBuildError("embedding 批次失敗") from error
            if len(vectors) != len(batch):
                raise IndexBuildError("embedding 回應筆數不符")
            if any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
                raise IndexBuildError(
                    f"embedding 向量維度必須為 {EMBEDDING_DIMENSION}"
                )
            with connection.transaction():
                for (chunk_id, _, content_sha256), vector in zip(
                    batch, vectors, strict=True
                ):
                    connection.execute(
                        """
                        INSERT INTO chunk_embeddings (
                            chunk_id,
                            index_build_id,
                            chunk_text_sha256,
                            embedding
                        ) VALUES (%s, %s, %s, %s::vector)
                        ON CONFLICT (chunk_id, index_build_id) DO UPDATE
                        SET chunk_text_sha256 = EXCLUDED.chunk_text_sha256,
                            embedding = EXCLUDED.embedding,
                            created_at = now()
                        """,
                        (
                            chunk_id,
                            build_id,
                            content_sha256,
                            _vector_literal(vector),
                        ),
                    )

        with connection.transaction():
            connection.execute("LOCK TABLE chunks IN SHARE MODE")
            total = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            complete = connection.execute(
                """
                SELECT count(*)
                FROM chunks
                JOIN chunk_embeddings
                  ON chunk_embeddings.chunk_id = chunks.chunk_id
                 AND chunk_embeddings.index_build_id = %s
                 AND chunk_embeddings.chunk_text_sha256 = chunks.content_sha256
                """,
                (build_id,),
            ).fetchone()[0]
            if complete != total:
                raise IndexBuildError("候選索引不完整")
            connection.execute(
                "UPDATE index_builds SET is_active = false WHERE is_active"
            )
            connection.execute(
                """
                UPDATE index_builds
                SET status = 'ready', is_active = true
                WHERE index_build_id = %s
                """,
                (build_id,),
            )
        return IndexBuildResult(build_id, complete)
    except Exception as error:
        with connection.transaction():
            connection.execute(
                """
                UPDATE index_builds
                SET status = 'failed', is_active = false
                WHERE index_build_id = %s
                """,
                (build_id,),
            )
        if isinstance(error, IndexBuildError):
            raise
        raise IndexBuildError("index build 失敗") from error


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(str(value) for value in vector) + "]"


def hybrid_search(
    connection: psycopg.Connection[Any],
    embeddings: EmbeddingBoundary,
    query: str,
    *,
    tickers: Sequence[str] | None = None,
    forms: Sequence[str] | None = None,
    period_from: str | date | None = None,
    period_to: str | date | None = None,
    filed_from: str | date | None = None,
    filed_to: str | date | None = None,
    sections: Sequence[str] | None = None,
    top_k: int = 8,
    cursor: str | None = None,
    candidate_limit: int = 50,
    rrf_k: int = 60,
) -> SearchPage:
    """以 active build 執行 metadata-first FTS、向量與 RRF 檢索。"""
    normalized_query = " ".join(query.split())
    if not normalized_query or len(normalized_query) > 2000:
        raise ValueError("query 長度必須介於 1 與 2000")
    normalized_tickers = _normalize_choices(tickers, ALLOWED_TICKERS, "tickers")
    normalized_forms = _normalize_choices(forms, ALLOWED_FORMS, "forms")
    normalized_sections = _normalize_choices(
        sections, ALLOWED_SECTIONS, "sections"
    )
    start_date = _parse_optional_date(period_from)
    end_date = _parse_optional_date(period_to)
    filed_start = _parse_optional_date(filed_from)
    filed_end = _parse_optional_date(filed_to)
    if start_date and end_date and start_date > end_date:
        raise ValueError("日期範圍無效")
    if filed_start and filed_end and filed_start > filed_end:
        raise ValueError("申報日期範圍無效")
    if not 1 <= top_k <= 12:
        raise ValueError("top_k 必須介於 1 與 12")
    if not 1 <= candidate_limit <= 50:
        raise ValueError("candidate_limit 必須介於 1 與 50")
    if rrf_k < 1:
        raise ValueError("rrf_k 必須大於 0")

    active_row = connection.execute(
        """
        SELECT index_build_id
        FROM index_builds
        WHERE is_active AND status = 'ready'
        """
    ).fetchone()
    if active_row is None:
        raise IndexUnavailableError("目前沒有 active index build")
    active_build_id = active_row[0]
    scope_hash = _retrieval_scope_hash(
        normalized_query,
        normalized_tickers,
        normalized_forms,
        start_date,
        end_date,
        filed_start,
        filed_end,
        normalized_sections,
        top_k,
    )
    offset = (
        _decode_cursor(cursor, active_build_id, scope_hash) if cursor is not None else 0
    )

    query_vectors = embeddings.embed([normalized_query], model=EMBEDDING_MODEL)
    if len(query_vectors) != 1 or len(query_vectors[0]) != EMBEDDING_DIMENSION:
        raise ValueError(f"查詢向量維度必須為 {EMBEDDING_DIMENSION}")
    rows = connection.execute(
        """
        WITH search_input AS (
            SELECT websearch_to_tsquery('english', %s) AS text_query,
                   %s::vector AS query_embedding
        ),
        active_build AS (
            SELECT index_build_id
            FROM index_builds
            WHERE index_build_id = %s AND is_active AND status = 'ready'
        ),
        eligible AS (
            SELECT
                chunks.chunk_id,
                active_build.index_build_id,
                companies.company_name,
                companies.cik,
                companies.ticker,
                filings.form,
                filings.filed_date,
                filings.period_end,
                filings.accession_number,
                sections.section_code,
                sections.section_title,
                sections.section_id,
                chunks.chunk_index,
                chunks.content_text,
                chunks.content_sha256,
                chunks.search_vector,
                filings.source_url,
                chunk_embeddings.embedding,
                lag(chunks.chunk_id) OVER (
                    PARTITION BY sections.section_id ORDER BY chunks.chunk_index
                ) AS previous_chunk_id,
                lead(chunks.chunk_id) OVER (
                    PARTITION BY sections.section_id ORDER BY chunks.chunk_index
                ) AS next_chunk_id
            FROM chunks
            JOIN filing_sections AS sections USING (section_id)
            JOIN filings USING (accession_number)
            JOIN companies USING (cik)
            CROSS JOIN active_build
            JOIN chunk_embeddings
              ON chunk_embeddings.chunk_id = chunks.chunk_id
             AND chunk_embeddings.index_build_id = active_build.index_build_id
             AND chunk_embeddings.chunk_text_sha256 = chunks.content_sha256
            WHERE (%s OR companies.ticker = ANY(%s::text[]))
              AND (%s OR filings.form = ANY(%s::text[]))
              AND filings.period_end >= COALESCE(%s::date, filings.period_end)
              AND filings.period_end <= COALESCE(%s::date, filings.period_end)
              AND filings.filed_date >= COALESCE(%s::date, filings.filed_date)
              AND filings.filed_date <= COALESCE(%s::date, filings.filed_date)
              AND (%s OR sections.section_code = ANY(%s::text[]))
        ),
        keyword_candidates AS (
            SELECT eligible.chunk_id,
                   row_number() OVER (
                       ORDER BY ts_rank_cd(
                           eligible.search_vector, search_input.text_query
                       ) DESC, eligible.chunk_id
                   )::integer AS keyword_rank
            FROM eligible
            CROSS JOIN search_input
            WHERE eligible.search_vector @@ search_input.text_query
            ORDER BY keyword_rank
            LIMIT %s
        ),
        vector_candidates AS (
            SELECT eligible.chunk_id,
                   row_number() OVER (
                       ORDER BY eligible.embedding <=> search_input.query_embedding,
                                eligible.chunk_id
                   )::integer AS vector_rank
            FROM eligible
            CROSS JOIN search_input
            ORDER BY vector_rank
            LIMIT %s
        ),
        fused AS (
            SELECT eligible.*,
                   keyword_candidates.keyword_rank,
                   vector_candidates.vector_rank,
                   COALESCE(
                       1.0 / (%s + keyword_candidates.keyword_rank), 0
                   ) + COALESCE(
                       1.0 / (%s + vector_candidates.vector_rank), 0
                   ) AS fusion_score
            FROM eligible
            LEFT JOIN keyword_candidates USING (chunk_id)
            LEFT JOIN vector_candidates USING (chunk_id)
            WHERE keyword_candidates.chunk_id IS NOT NULL
               OR vector_candidates.chunk_id IS NOT NULL
        ),
        ranked AS (
            SELECT fused.*,
                   row_number() OVER (
                       ORDER BY fusion_score DESC, chunk_id
                   )::integer AS fusion_rank
            FROM fused
        )
        SELECT
            chunk_id, index_build_id, company_name, cik, ticker, form,
            filed_date, period_end, accession_number, section_code,
            section_title, chunk_index, content_text, content_sha256,
            source_url, previous_chunk_id, next_chunk_id, keyword_rank,
            vector_rank, fusion_rank
        FROM ranked
        ORDER BY fusion_rank
        """,
        (
            normalized_query,
            _vector_literal(query_vectors[0]),
            active_build_id,
            not normalized_tickers,
            list(normalized_tickers),
            not normalized_forms,
            list(normalized_forms),
            start_date,
            end_date,
            filed_start,
            filed_end,
            not normalized_sections,
            list(normalized_sections),
            candidate_limit,
            candidate_limit,
            rrf_k,
            rrf_k,
        ),
    ).fetchall()
    selected = rows[offset : offset + top_k]
    results = tuple(SearchResult(*row) for row in selected)
    next_offset = offset + len(selected)
    next_cursor = (
        _encode_cursor(active_build_id, scope_hash, next_offset)
        if next_offset < len(rows)
        else None
    )
    return SearchPage(results, active_build_id, next_cursor)


def resolve_citation(result: SearchResult) -> Citation:
    """將單筆檢索結果解析成可驗證且只指向 SEC 的引用。"""
    try:
        source_url = validate_sec_url(result.source_url)
    except SecClientError as error:
        raise CitationError("引用 URL 必須指向 SEC 官方主機") from error
    if result.accession_number.replace("-", "") not in source_url:
        raise CitationError("引用 URL 與 accession 不一致")
    citation_id = hashlib.sha256(
        f"{result.index_build_id}:{result.chunk_id}".encode("utf-8")
    ).hexdigest()[:24]
    return Citation(
        citation_id=citation_id,
        company_name=result.company_name,
        ticker=result.ticker,
        form=result.form,
        period_end=result.period_end,
        filing_date=result.filing_date,
        accession_number=result.accession_number,
        section_code=result.section_code,
        source_url=source_url,
    )


def evaluate_retrieval(
    connection: psycopg.Connection[Any],
    embeddings: EmbeddingBoundary,
    cases: Sequence[dict[str, Any]],
) -> RetrievalEvaluation:
    """對固定案例計算 metadata、Recall@8 與 SEC 引用指標。"""
    if len(cases) < 6:
        raise ValueError("RAG 評估至少需要 6 個案例")
    failures: list[str] = []
    metadata_passed = 0
    expected_total = 0
    recalled_total = 0
    citation_total = 0
    valid_citations = 0

    for case in cases:
        case_id = str(case.get("id", "unknown"))
        filters = dict(case.get("filters", {}))
        expected = list(case.get("expected", []))
        page = hybrid_search(
            connection,
            embeddings,
            str(case.get("query", "")),
            top_k=8,
            **filters,
        )
        actual_keys = {
            (result.accession_number, result.section_code) for result in page.results
        }
        expected_keys = {
            (item["accession_number"], item["section_code"]) for item in expected
        }
        expected_total += len(expected_keys)
        recalled_total += len(actual_keys & expected_keys)

        metadata_ok = all(_result_matches_filters(result, filters) for result in page.results)
        if not expected and page.results:
            metadata_ok = False
            failures.append(f"{case_id}: 預期無結果但收到檢索資料")
        if metadata_ok:
            metadata_passed += 1
        else:
            failures.append(f"{case_id}: metadata 範圍不符")
        if not expected_keys <= actual_keys:
            failures.append(f"{case_id}: Recall@8 未取回所有預期結果")

        expected_by_key = {
            (item["accession_number"], item["section_code"]): item
            for item in expected
        }
        for result in page.results:
            citation_total += 1
            try:
                citation = resolve_citation(result)
            except CitationError:
                failures.append(f"{case_id}: citation URL 無法解析")
                continue
            valid_citations += 1
            expected_citation = expected_by_key.get(
                (result.accession_number, result.section_code)
            )
            if expected_citation and not _citation_matches_expected(
                citation, expected_citation
            ):
                failures.append(f"{case_id}: citation metadata 與預期不符")

    case_count = len(cases)
    metadata_accuracy = metadata_passed / case_count
    recall = recalled_total / expected_total if expected_total else 1.0
    citation_accuracy = valid_citations / citation_total if citation_total else 1.0
    return RetrievalEvaluation(
        case_count=case_count,
        metadata_accuracy=metadata_accuracy,
        recall_at_8=recall,
        citation_url_accuracy=citation_accuracy,
        failures=tuple(failures),
        passed=(
            metadata_accuracy == 1.0
            and recall >= 0.85
            and citation_accuracy == 1.0
            and not failures
        ),
    )


def _result_matches_filters(result: SearchResult, filters: dict[str, Any]) -> bool:
    tickers = {value.upper() for value in filters.get("tickers", [])}
    forms = {value.upper() for value in filters.get("forms", [])}
    sections = {value.upper() for value in filters.get("sections", [])}
    period_from = _parse_optional_date(filters.get("period_from"))
    period_to = _parse_optional_date(filters.get("period_to"))
    return (
        (not tickers or result.ticker in tickers)
        and (not forms or result.form in forms)
        and (not sections or result.section_code in sections)
        and (period_from is None or result.period_end >= period_from)
        and (period_to is None or result.period_end <= period_to)
    )


def _citation_matches_expected(
    citation: Citation,
    expected: dict[str, Any],
) -> bool:
    values: dict[str, Any] = {
        "ticker": citation.ticker,
        "form": citation.form,
        "filing_date": citation.filing_date.isoformat(),
        "period_end": citation.period_end.isoformat(),
        "accession_number": citation.accession_number,
        "section_code": citation.section_code,
        "source_url": citation.source_url,
    }
    return all(values.get(key) == value for key, value in expected.items())


def _normalize_choices(
    values: Sequence[str] | None,
    allowed: frozenset[str],
    field: str,
) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip().upper() for value in values or ()}))
    if values is not None and (not normalized or not set(normalized) <= allowed):
        raise ValueError(f"{field} 含有不支援的值")
    return normalized


def _parse_optional_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("日期必須使用 YYYY-MM-DD") from error


def _retrieval_scope_hash(
    query: str,
    tickers: tuple[str, ...],
    forms: tuple[str, ...],
    period_from: date | None,
    period_to: date | None,
    filed_from: date | None,
    filed_to: date | None,
    sections: tuple[str, ...],
    top_k: int,
) -> str:
    value = json.dumps(
        {
            "query": query,
            "tickers": tickers,
            "forms": forms,
            "period_from": period_from.isoformat() if period_from else None,
            "period_to": period_to.isoformat() if period_to else None,
            "filed_from": filed_from.isoformat() if filed_from else None,
            "filed_to": filed_to.isoformat() if filed_to else None,
            "sections": sections,
            "top_k": top_k,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode_cursor(index_build_id: UUID, scope_hash: str, offset: int) -> str:
    payload = json.dumps(
        {"build": str(index_build_id), "scope": scope_hash, "offset": offset},
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


def _decode_cursor(cursor: str, index_build_id: UUID, scope_hash: str) -> int:
    try:
        envelope_bytes = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        envelope = json.loads(envelope_bytes)
        payload = base64.urlsafe_b64decode(envelope["payload"])
        decoded = json.loads(payload)
        checksum = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(envelope["checksum"], checksum):
            raise ValueError
        if decoded["build"] != str(index_build_id) or decoded["scope"] != scope_hash:
            raise ValueError
        offset = decoded["offset"]
        if not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("cursor 無效或不屬於目前查詢") from error


def persist_sections_and_chunks(
    connection: psycopg.Connection[Any],
    filing: FilingMetadata,
    sections: tuple[ParsedSection, ...],
) -> dict[str, int]:
    chunk_count = 0
    with connection.transaction():
        for section in sections:
            section_digest = hashlib.sha256(section.content.encode("utf-8")).hexdigest()
            proposed_section_id = uuid5(
                NAMESPACE_URL,
                f"{filing.source_url}#{section.code}#{section.ordinal}",
            )
            section_id = connection.execute(
                """
                INSERT INTO filing_sections (
                    section_id,
                    accession_number,
                    section_code,
                    section_title,
                    ordinal,
                    content_text,
                    content_sha256,
                    parse_confidence,
                    parse_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'parsed')
                ON CONFLICT (accession_number, section_code, ordinal) DO UPDATE
                SET section_title = EXCLUDED.section_title,
                    content_text = EXCLUDED.content_text,
                    content_sha256 = EXCLUDED.content_sha256,
                    parse_confidence = EXCLUDED.parse_confidence,
                    parse_status = EXCLUDED.parse_status
                RETURNING section_id
                """,
                (
                    proposed_section_id,
                    filing.accession_number,
                    section.code,
                    section.title,
                    section.ordinal,
                    section.content,
                    section_digest,
                    section.confidence,
                ),
            ).fetchone()[0]
            chunks = chunk_section(filing, section)
            for chunk in chunks:
                connection.execute(
                    """
                    INSERT INTO chunks (
                        chunk_id,
                        section_id,
                        chunk_index,
                        content_text,
                        token_count,
                        content_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (section_id, chunk_index) DO UPDATE
                    SET content_text = EXCLUDED.content_text,
                        token_count = EXCLUDED.token_count,
                        content_sha256 = EXCLUDED.content_sha256
                    """,
                    (
                        chunk.chunk_id,
                        section_id,
                        chunk.chunk_index,
                        chunk.content_text,
                        chunk.token_count,
                        chunk.content_sha256,
                    ),
                )
            connection.execute(
                "DELETE FROM chunks WHERE section_id = %s AND chunk_index >= %s",
                (section_id, len(chunks)),
            )
            chunk_count += len(chunks)
        connection.execute(
            """
            UPDATE filings
            SET processing_status = 'parsed', parser_version = 'v1', updated_at = now()
            WHERE accession_number = %s
            """,
            (filing.accession_number,),
        )
    return {"sections": len(sections), "chunks": chunk_count}
