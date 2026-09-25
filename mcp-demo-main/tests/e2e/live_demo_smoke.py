"""明確執行的正式 MCP 與 Demo smoke；不屬於預設離線 pytest。"""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI
from streamlit.testing.v1 import AppTest

from sec_research.sec_client import validate_sec_url


QUESTIONS = (
    ("敘事", "比較 AAPL 最近五個已完成會計年度的 Risk Factors 變化。", {"search_filing_sections"}),
    ("數值", "列出 MSFT 最近五個已完成會計年度的營收趨勢。", {"get_financial_metric"}),
    ("複合", "NVDA 的資料中心營收成長與 Risk Factors 變化有何關聯？", {
        "search_filing_sections", "get_financial_metric"
    }),
)
TOOLS = {
    "list_filings",
    "get_corpus_status",
    "get_latest_filings",
    "search_filing_sections",
    "read_filing_section",
    "get_financial_metric",
}
ACCESSION = "0000320193-25-000079"


async def check_mcp() -> dict[str, object]:
    url = os.environ.get("MCP_URL", "http://mcp:8000/mcp")
    base = url.removesuffix("/mcp")
    assert httpx.get(f"{base}/health/live", timeout=10).json()["status"] == "live"
    assert httpx.get(f"{base}/health/ready", timeout=10).json()["status"] == "ready"
    async with streamable_http_client(url) as streams:
        async with ClientSession(streams[0], streams[1], read_timeout_seconds=60) as client:
            await client.initialize()
            listed = await client.list_tools()
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            names = {item.name for item in listed.tools}
            assert names == TOOLS, names
            assert {str(item.uri) for item in resources.resources} == {
                "sec://corpus/status"
            }
            assert {item.uri_template for item in templates.resource_templates} == {
                "sec://filings/{accession_number}",
                "sec://filings/{accession_number}/sections/{section_code}",
            }
            calls = {
                "list_filings": {"tickers": ["AAPL"], "forms": ["10-K"], "limit": 1},
                "get_corpus_status": {},
                "get_latest_filings": {"ticker": "AAPL", "limit": 1},
                "search_filing_sections": {
                    "query": "supply chain risk", "tickers": ["AAPL"],
                    "forms": ["10-K"], "sections": ["ITEM_1A"], "top_k": 1,
                },
                "read_filing_section": {
                    "accession_number": ACCESSION,
                    "section_code": "ITEM_1A", "max_chars": 1000,
                },
                "get_financial_metric": {
                    "ticker": "MSFT",
                    "concepts": ["RevenueFromContractWithCustomerExcludingAssessedTax"],
                    "forms": ["10-K"], "unit": "USD",
                    "period_from": "2025-06-30", "period_to": "2025-06-30",
                },
            }
            statuses = {}
            for name, arguments in calls.items():
                result = await client.call_tool(name, arguments)
                payload = result.structured_content
                assert not result.is_error and isinstance(payload, dict), name
                assert payload["status"] in {"ok", "partial"}, (name, payload["status"])
                statuses[name] = payload["status"]
                for citation in payload.get("citations", []):
                    validate_sec_url(citation["source_url"])
            for uri in (
                "sec://corpus/status",
                f"sec://filings/{ACCESSION}",
                f"sec://filings/{ACCESSION}/sections/ITEM_1A",
            ):
                result = await client.read_resource(uri)
                assert result.contents, uri
                payload = json.loads(result.contents[0].text)
                assert payload["status"] == "ok", uri
    return {"tools": statuses, "resources_read": 3}


def check_demo() -> list[dict[str, object]]:
    app_path = Path("/app/src/sec_research/app.py")
    records = []
    for category, question, required_tools in QUESTIONS:
        if "--composite-only" in sys.argv and category != "複合":
            continue
        started = time.perf_counter()
        app = AppTest.from_file(app_path, default_timeout=300).run()
        assert not app.exception and not app.error, (
            category, [item.value for item in app.error], len(app.exception)
        )
        assert any(button.label == question for button in app.button), category
        app.text_area[0].input(question).run()
        app = next(
            button for button in app.button if button.label == "開始研究"
        ).click().run()
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if app.error:
            from sec_research.app import _run_live_research
            from sec_research.config import Settings

            try:
                asyncio.run(_run_live_research(Settings(), question))
            except Exception as error:
                def error_types(item: BaseException) -> object:
                    children = getattr(item, "exceptions", ())
                    return (
                        type(item).__name__,
                        [error_types(child) for child in children],
                    )

                raise AssertionError((
                    category, error_types(error),
                )) from None
        assert not app.exception and not app.error, (
            category, [item.value for item in app.error], len(app.exception)
        )
        headings = {item.value for item in app.subheader}
        print(json.dumps({
            "category": category, "headings": sorted(headings),
            "links": len(app.get("link_button")),
            "warnings": len(app.warning),
            "trace_names": list(app.dataframe[0].value["name"]) if app.dataframe else [],
        }, ensure_ascii=False), flush=True)
        assert "代理綜合結論" in headings and "工具軌跡" in headings
        if category in {"敘事", "複合"}:
            assert "RAG 敘事證據" in headings, category
        if category in {"數值", "複合"}:
            assert "XBRL 數值證據" in headings, category
        rendered = "\n".join(item.value for item in app.markdown)
        assert re.search(r"[\u4e00-\u9fff]", rendered), category
        assert "研究資訊，非投資建議" in rendered, category
        urls = [item.proto.url for item in app.get("link_button")]
        assert urls, category
        for url in urls:
            validate_sec_url(url)
        assert app.dataframe, category
        traces = app.dataframe[0].value
        assert required_tools <= set(traces["name"]), (category, list(traces["name"]))
        assert all(value >= 0 for value in traces["duration_ms"])
        assert all(not item.proto.allow_html for item in app.markdown)
        records.append({
            "category": category,
            "elapsed_ms": elapsed_ms,
            "tool_names": list(traces["name"]),
            "tool_duration_ms": list(traces["duration_ms"]),
            "citation_count": len(urls),
            "warning_count": len(app.warning),
        })
    return records


def check_model() -> list[dict[str, object]]:
    from sec_research.app import _run_live_research
    from sec_research.config import Settings

    records = []
    for category, question, required_tools in QUESTIONS:
        if "--composite-only" in sys.argv and category != "複合":
            continue
        started = time.perf_counter()
        result = asyncio.run(_run_live_research(Settings(), question))
        traces = result["tool_traces"]
        used_tools = {trace["name"] for trace in traces}
        assert re.search(r"[\u4e00-\u9fff]", result["answer_markdown"]), category
        assert "研究資訊，非投資建議" in result["answer_markdown"], category
        for citation in result["citations"]:
            validate_sec_url(citation["source_url"])
        record = {
            "category": category,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "tool_names": [trace["name"] for trace in traces],
            "tool_duration_ms": [trace["duration_ms"] for trace in traces],
            "citation_count": len(result["citations"]),
            "warning_count": len(result["warnings"]),
            "is_complete": result["is_complete"],
        }
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        assert required_tools <= used_tools, (category, used_tools)
    return records


async def diagnose_model() -> None:
    from sec_research.agent import build_openai_tools, run_research

    model = AsyncOpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
    )
    events = []
    async with streamable_http_client(os.environ["MCP_URL"]) as streams:
        async with ClientSession(streams[0], streams[1], read_timeout_seconds=60) as client:
            await client.initialize()
            tools = await build_openai_tools(client)

            class RecordingClient:
                async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
                    result = await client.call_tool(name, arguments)
                    payload = result.structured_content or {}
                    events.append({
                        "name": name,
                        "arguments": {
                            key: value for key, value in arguments.items()
                            if key in {"tickers", "forms", "sections", "top_k", "max_chars"}
                        },
                        "result_chars": len(json.dumps(payload, ensure_ascii=False)),
                    })
                    print(json.dumps(events[-1], ensure_ascii=False), flush=True)
                    return result

            try:
                await run_research(
                    model.responses, RecordingClient(), QUESTIONS[0][1],
                    tools=tools, context_budget_chars=200_000,
                )
            except Exception as error:
                print(json.dumps({
                    "error_type": type(error).__name__, "events": events,
                }, ensure_ascii=False), flush=True)
                raise
    await model.close()


def main() -> None:
    assert os.environ.get("OPENROUTER_API_KEY"), "需要正式 OpenRouter 設定"
    if "--diagnose" in sys.argv:
        asyncio.run(diagnose_model())
        return
    mcp = asyncio.run(check_mcp())
    demo = check_model() if "--model-only" in sys.argv else check_demo()
    print(json.dumps({"mcp": mcp, "demo": demo}, ensure_ascii=False))


if __name__ == "__main__":
    main()
