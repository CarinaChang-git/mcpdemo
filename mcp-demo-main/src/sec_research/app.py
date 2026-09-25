"""SEC Filing Research Agent 的單頁 Streamlit Demo。"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI, OpenAIError
from pydantic import ValidationError
import streamlit as st

from sec_research.agent import CitationValidationError, ResearchLoopResult, run_research
from sec_research.config import Settings
from sec_research.sec_client import SecClientError, validate_sec_url


BUILTIN_QUESTIONS = (
    "比較 AAPL 最近五個已完成會計年度的 Risk Factors 變化。",
    "列出 MSFT 最近五個已完成會計年度的營收趨勢。",
    "NVDA 的資料中心營收成長與 Risk Factors 變化有何關聯？",
)
ERROR_MESSAGES = {
    "CORPUS_UNAVAILABLE": "語料庫尚未就緒，請先完成資料建置與索引。",
    "MCP_UNAVAILABLE": "MCP Server 無法連線，請確認服務與 readiness。",
    "MODEL_UNAVAILABLE": "研究模型暫時無法使用，請稍後再試。",
    "SEC_UNAVAILABLE": "SEC 即時資料暫時無法取得，請稍後再試。",
    "EVIDENCE_INVALID": "證據驗證失敗，未顯示未受支持的結論。",
    "CONFIG_UNAVAILABLE": "必要設定不完整，請檢查環境變數。",
}
TRACE_FIELDS = ("name", "purpose", "status", "duration_ms", "result_count")


class DemoServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _load_fixture() -> dict[str, Any] | None:
    path = os.getenv("SEC_RESEARCH_DEMO_FIXTURE")
    if not path:
        return None
    with Path(path).open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise DemoServiceError("CONFIG_UNAVAILABLE")
    return payload


def load_status() -> dict[str, Any]:
    fixture = _load_fixture()
    if fixture is not None:
        return fixture["status"]
    try:
        settings = Settings()
    except ValidationError:
        code = "CONFIG_UNAVAILABLE"
    else:
        if settings.openrouter_api_key is None:
            code = "CONFIG_UNAVAILABLE"
        else:
            try:
                return asyncio.run(_load_live_status(settings))
            except DemoServiceError as error:
                code = error.code
            except Exception:
                code = "MCP_UNAVAILABLE"
    return {
        "ready": False,
        "mcp_status": "not_ready",
        "model_status": "not_ready",
        "scope": {},
        "last_sync_at": None,
        "active_index_build": None,
        "error": code,
    }


def research(question: str) -> dict[str, Any]:
    fixture = _load_fixture()
    if fixture is not None:
        error = fixture.get("errors", {}).get(question)
        if error:
            raise DemoServiceError(error)
        response = fixture.get("responses", {}).get(question)
        if not isinstance(response, dict):
            raise DemoServiceError("EVIDENCE_INVALID")
        return response
    try:
        settings = Settings()
    except ValidationError as error:
        raise DemoServiceError("CONFIG_UNAVAILABLE") from error
    try:
        return asyncio.run(_run_live_research(settings, question))
    except CitationValidationError as error:
        raise DemoServiceError("EVIDENCE_INVALID") from error
    except OpenAIError as error:
        raise DemoServiceError("MODEL_UNAVAILABLE") from error
    except DemoServiceError:
        raise
    except Exception as error:
        raise DemoServiceError("MCP_UNAVAILABLE") from error


async def _load_live_status(settings: Settings) -> dict[str, Any]:
    async with streamable_http_client(str(settings.mcp_url)) as streams:
        async with ClientSession(
            streams[0],
            streams[1],
            read_timeout_seconds=settings.mcp_request_deadline_seconds,
        ) as client:
            await client.initialize()
            result = await client.call_tool("get_corpus_status", {})
    payload = getattr(result, "structured_content", None)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise DemoServiceError("CORPUS_UNAVAILABLE")
    data = payload.get("data", {})
    active = data.get("active_index_build")
    if active is not None:
        await _check_model_dependency(settings)
    return {
        "ready": active is not None,
        "mcp_status": "ready",
        "model_status": "ready" if active is not None else "not_checked",
        "scope": data.get("scope", {}),
        "last_sync_at": data.get("last_sync_at"),
        "active_index_build": (
            active.get("index_build_id") if isinstance(active, dict) else None
        ),
        "error": None if active is not None else "CORPUS_UNAVAILABLE",
    }


async def _check_model_dependency(settings: Settings) -> None:
    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url="https://openrouter.ai/api/v1",
        timeout=10,
    )
    try:
        models = await client.models.list()
    except OpenAIError as error:
        raise DemoServiceError("MODEL_UNAVAILABLE") from error
    finally:
        await client.close()
    if not any(
        getattr(item, "id", None) == settings.openrouter_answer_model
        for item in getattr(models, "data", [])
    ):
        raise DemoServiceError("MODEL_UNAVAILABLE")


async def _run_live_research(
    settings: Settings,
    question: str,
) -> dict[str, Any]:
    if settings.openrouter_api_key is None:
        raise DemoServiceError("CONFIG_UNAVAILABLE")
    openrouter_client = AsyncOpenAI(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url="https://openrouter.ai/api/v1",
    )
    try:
        async with streamable_http_client(str(settings.mcp_url)) as streams:
            async with ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=settings.mcp_request_deadline_seconds,
            ) as mcp_client:
                await mcp_client.initialize()
                result = await run_research(
                    openrouter_client.responses,
                    mcp_client,
                    question,
                    model=settings.openrouter_answer_model,
                    max_tool_calls=settings.agent_max_tool_calls,
                    context_budget_chars=settings.agent_context_budget_chars,
                    deadline_seconds=settings.agent_deadline_seconds,
                )
    finally:
        await openrouter_client.close()
    return _result_payload(result)


def _result_payload(result: ResearchLoopResult) -> dict[str, Any]:
    traces = getattr(result, "tool_traces", ())
    return {
        "answer_markdown": result.answer_markdown,
        "citation_ids": list(result.citation_ids),
        "warnings": list(result.warnings),
        "is_complete": result.is_complete,
        "citations": list(result.citations),
        "tool_traces": [
            {
                field: getattr(trace, field)
                for field in TRACE_FIELDS
            }
            for trace in traces
        ],
    }


def render_status(status: dict[str, Any]) -> None:
    scope = status.get("scope", {})
    tickers = "、".join(scope.get("tickers", [])) or "尚無資料"
    forms = "、".join(scope.get("forms", [])) or "尚無資料"
    st.markdown(f"**Corpus scope：** {tickers}｜{forms}")
    columns = st.columns(4)
    columns[0].metric("MCP readiness", status.get("mcp_status", "unknown"))
    columns[1].metric("模型依賴", status.get("model_status", "unknown"))
    columns[2].metric("最後同步", status.get("last_sync_at") or "尚無資料")
    columns[3].metric("Active index", status.get("active_index_build") or "尚無")


def render_result(result: dict[str, Any], scope: dict[str, Any]) -> None:
    st.subheader("代理綜合結論")
    tickers = "、".join(scope.get("tickers", []))
    forms = "、".join(scope.get("forms", []))
    answer = str(result.get("answer_markdown", ""))
    if tickers and forms:
        answer = (
            "未指定條件時的 Demo 語料邊界："
            f"{tickers}｜{forms}｜{scope.get('period_from') or '未定'}"
            f" 至 {scope.get('period_to') or '未定'}。\n\n{answer}"
        )
    st.markdown(answer, unsafe_allow_html=False)
    st.markdown("研究資訊，非投資建議。", unsafe_allow_html=False)
    if not result.get("is_complete", False):
        st.warning("現有證據不足以支持完整結論。")
    warnings = [str(item) for item in result.get("warnings", [])]
    if any(
        item in {
            "TOOL_ERROR:DEPENDENCY_UNAVAILABLE",
            "TOOL_ERROR:RATE_LIMITED",
        }
        for item in warnings
    ):
        st.error(ERROR_MESSAGES["SEC_UNAVAILABLE"])
    for warning in warnings:
        if not warning.startswith(("TOOL_ERROR:", "TOOL_STATUS:")):
            st.warning(warning)

    citations = result.get("citations", [])
    rag = [item for item in citations if item.get("section_code")]
    xbrl = [item for item in citations if item.get("concept")]
    if rag:
        st.subheader("RAG 敘事證據")
        for citation in rag:
            render_citation(citation)
    if xbrl:
        st.subheader("XBRL 數值證據")
        for citation in xbrl:
            render_citation(citation)

    traces = [
        {field: trace.get(field) for field in TRACE_FIELDS}
        for trace in result.get("tool_traces", [])
        if isinstance(trace, dict)
    ]
    st.subheader("工具軌跡")
    if traces:
        st.dataframe(traces, hide_index=True, width="stretch")
    else:
        st.caption("本次回答沒有工具呼叫。")


def render_citation(citation: dict[str, Any]) -> None:
    source_url = citation.get("source_url")
    if not isinstance(source_url, str):
        st.error(ERROR_MESSAGES["EVIDENCE_INVALID"])
        return
    try:
        validate_sec_url(source_url)
    except SecClientError:
        st.error(ERROR_MESSAGES["EVIDENCE_INVALID"])
        return
    label = "｜".join(
        str(value)
        for value in (
            citation.get("ticker"),
            citation.get("form"),
            citation.get("period_end") or citation.get("end_date"),
            citation.get("section_code") or citation.get("concept"),
        )
        if value
    )
    st.markdown(f"**{label}**", unsafe_allow_html=False)
    fields = (
        ("申報日", "filing_date"),
        ("Accession", "accession_number"),
        ("Taxonomy", "taxonomy"),
        ("數值", "value"),
        ("單位", "unit"),
        ("會計年度", "fiscal_year"),
        ("會計期間", "fiscal_period"),
    )
    details = "｜".join(
        f"{title}：{citation[key]}"
        for title, key in fields
        if citation.get(key) is not None
    )
    if details:
        st.caption(details)
    st.code(str(citation.get("excerpt") or "無可用短摘"), language=None)
    st.link_button("在 SEC 查看原始文件", source_url)


def main() -> None:
    st.set_page_config(page_title="SEC Filing Research Agent", layout="wide")
    st.title("SEC Filing Research Agent")
    st.caption("以 SEC 官方 filing 與 XBRL facts 為唯一研究來源")

    status = load_status()
    render_status(status)
    if not status.get("ready", False):
        code = status.get("error", "CORPUS_UNAVAILABLE")
        st.error(ERROR_MESSAGES.get(code, ERROR_MESSAGES["CORPUS_UNAVAILABLE"]))

    st.subheader("內建研究問題")
    columns = st.columns(3)
    for column, question in zip(columns, BUILTIN_QUESTIONS, strict=True):
        if column.button(question, width="stretch"):
            st.session_state["research_question"] = question

    question = st.text_area(
        "研究問題",
        key="research_question",
        max_chars=4_000,
        placeholder="輸入公司、期間與想研究的 filing 問題",
    )
    submitted = st.button(
        "開始研究",
        type="primary",
        disabled=not status.get("ready", False),
    )
    if submitted and question.strip():
        try:
            with st.spinner("正在查詢 SEC 證據並驗證引用……"):
                result = research(question.strip())
            render_result(result, status.get("scope", {}))
        except DemoServiceError as error:
            st.error(ERROR_MESSAGES.get(error.code, "研究服務暫時無法使用。"))


if __name__ == "__main__":
    main()
