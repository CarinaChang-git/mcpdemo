import asyncio
from dataclasses import dataclass
import json
from typing import Any

import pytest

from sec_research.agent import (
    AgentProtocolError,
    ContextBudgetExceeded,
    ResearchTimeout,
    ToolBudgetExceeded,
    run_research,
)


TOOLS = [
    {
        "type": "function",
        "name": "search_filing_sections",
        "description": "搜尋 SEC filing 章節",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "top_k": {"type": ["integer", "null"], "minimum": 1, "maximum": 12},
            },
            "required": ["query", "top_k"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_financial_metric",
        "description": "讀取 XBRL facts",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "enum": ["AAPL", "MSFT", "NVDA"]},
                "concepts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
            },
            "required": ["ticker", "concepts"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def answer_json(answer_markdown: str) -> str:
    return json.dumps(
        {
            "answer_markdown": answer_markdown,
            "citation_ids": [],
            "warnings": [],
            "is_complete": True,
        },
        ensure_ascii=False,
    )


@dataclass
class FakeResponse:
    output: list[dict[str, Any]]
    output_text: str = ""
    id: str = "response-id"


class FakeResponses:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeMcpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"status": "ok", "data": {"tool": name}, "citations": []}


@pytest.mark.asyncio
async def test_zero_tool_response_uses_required_model_and_sequential_mode() -> None:
    responses = FakeResponses([FakeResponse([], answer_json("直接答案"))])
    mcp = FakeMcpClient()

    result = await run_research(responses, mcp, "問題", tools=TOOLS)

    assert result.answer_text.startswith("直接答案")
    assert result.tool_calls == ()
    assert mcp.calls == []
    assert responses.calls[0]["model"] == "openai/gpt-6-luna"
    assert responses.calls[0]["parallel_tool_calls"] is False
    assert responses.calls[0]["tools"] == TOOLS
    assert responses.calls[0]["text"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_cross_period_question_first_checks_filing_inventory() -> None:
    list_tool = {
        "type": "function",
        "name": "list_filings",
        "parameters": {
            "type": "object",
            "properties": {"tickers": {"type": ["array", "null"], "items": {"type": "string"}}},
            "required": ["tickers"],
            "additionalProperties": False,
        },
    }
    responses = FakeResponses([
        FakeResponse([{
            "type": "function_call", "name": "list_filings", "call_id": "inventory",
            "arguments": '{"tickers":["AAPL"]}',
        }]),
        FakeResponse([], answer_json("證據不足")),
    ])
    mcp = FakeMcpClient()

    result = await run_research(
        responses, mcp, "比較 AAPL 最近五個已完成會計年度的 Risk Factors 變化。",
        tools=[list_tool, *TOOLS],
    )

    assert responses.calls[0]["tool_choice"] == {"type": "function", "name": "list_filings"}
    assert result.tool_calls[0] == "list_filings"
    assert mcp.calls[0] == ("list_filings", {"tickers": ["AAPL"]})


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_year", [None, 2023])
async def test_cross_period_answer_lists_found_and_missing_filings(missing_year: int | None) -> None:
    list_tool = {
        "type": "function", "name": "list_filings",
        "parameters": {
            "type": "object", "properties": {"tickers": {"type": "array", "items": {"type": "string"}}},
            "required": ["tickers"], "additionalProperties": False,
        },
    }
    filings = [
        {
            "ticker": "AAPL", "form": "10-K", "period_end": f"{year}-09-30",
            "accession_number": f"0000320193-{year % 100:02d}-000010",
            "source_url": (
                "https://www.sec.gov/Archives/edgar/data/320193/"
                f"0000320193{year % 100:02d}000010/aapl-{year}0930.htm"
            ),
        }
        for year in range(2021, 2026) if year != missing_year
    ]

    class FilingMcpClient(FakeMcpClient):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, arguments))
            return {"status": "ok", "data": {"filings": filings}, "citations": []}

    responses = FakeResponses([
        FakeResponse([{
            "type": "function_call", "name": "list_filings", "call_id": "inventory",
            "arguments": '{"tickers":["AAPL"]}',
        }]),
        FakeResponse([], answer_json("申報清單查核結果。")),
    ])

    result = await run_research(
        responses, FilingMcpClient(), "列出 AAPL 最近五個已完成會計年度的 filing。",
        tools=[list_tool, *TOOLS],
    )

    for year in range(2021, 2026):
        assert f"AAPL {year} 年" in result.answer_markdown
    assert ("缺少" in result.answer_markdown) is (missing_year is not None)
    assert result.is_complete is (missing_year is None)
    assert len(result.citations) == len(filings)


@pytest.mark.asyncio
async def test_single_tool_preserves_output_items_and_call_id() -> None:
    responses = FakeResponses(
        [
            FakeResponse(
                [
                    {"type": "reasoning", "id": "reason-1", "summary": []},
                    {
                        "type": "function_call",
                        "name": "search_filing_sections",
                        "call_id": "call-1",
                        "arguments": '{"query":"risk","top_k":null}',
                    },
                ]
            ),
            FakeResponse([], answer_json("完成")),
        ]
    )
    mcp = FakeMcpClient()

    result = await run_research(responses, mcp, "風險？", tools=TOOLS)

    assert result.is_complete is False
    assert "證據不足" in result.warnings
    assert len(result.tool_traces) == 1
    trace = result.tool_traces[0]
    assert trace.name == "search_filing_sections"
    assert trace.purpose == "搜尋 SEC filing 敘事章節"
    assert trace.status == "ok"
    assert trace.duration_ms >= 0
    assert trace.result_count == 1
    assert result.tool_calls == ("search_filing_sections",)
    assert mcp.calls == [("search_filing_sections", {"query": "risk"})]
    second_input = responses.calls[1]["input"]
    assert second_input[1]["type"] == "reasoning"
    assert second_input[2]["type"] == "function_call"
    assert second_input[3]["type"] == "function_call_output"
    assert second_input[3]["call_id"] == "call-1"


@pytest.mark.asyncio
async def test_multi_round_rag_and_xbrl_are_called_sequentially() -> None:
    responses = FakeResponses(
        [
            FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "search_filing_sections",
                        "call_id": "rag",
                        "arguments": '{"query":"revenue","top_k":8}',
                    }
                ]
            ),
            FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "get_financial_metric",
                        "call_id": "xbrl",
                        "arguments": '{"ticker":"AAPL","concepts":["Revenues"]}',
                    }
                ]
            ),
            FakeResponse([], answer_json("綜合答案")),
        ]
    )
    mcp = FakeMcpClient()

    result = await run_research(responses, mcp, "比較趨勢與數值", tools=TOOLS)

    assert result.tool_calls == (
        "search_filing_sections",
        "get_financial_metric",
    )
    assert len(responses.calls) == 3
    assert all(call["parallel_tool_calls"] is False for call in responses.calls)


@pytest.mark.asyncio
async def test_unknown_tool_invalid_arguments_and_budget_are_rejected() -> None:
    unknown = FakeResponses(
        [
            FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "shell",
                        "call_id": "bad",
                        "arguments": "{}",
                    }
                ]
            )
        ]
    )
    with pytest.raises(AgentProtocolError, match="未知工具"):
        await run_research(unknown, FakeMcpClient(), "問題", tools=TOOLS)

    invalid = FakeResponses(
        [
            FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "get_financial_metric",
                        "call_id": "bad-args",
                        "arguments": '{"ticker":"TSLA","concepts":[]}',
                    }
                ]
            )
        ]
    )
    with pytest.raises(AgentProtocolError, match="參數"):
        await run_research(invalid, FakeMcpClient(), "問題", tools=TOOLS)

    budget = FakeResponses(
        [
            FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "search_filing_sections",
                        "call_id": str(index),
                        "arguments": '{"query":"risk","top_k":8}',
                    }
                ]
            )
            for index in range(2)
        ]
    )
    with pytest.raises(ToolBudgetExceeded):
        await run_research(
            budget,
            FakeMcpClient(),
            "問題",
            tools=TOOLS,
            max_tool_calls=1,
        )


@pytest.mark.asyncio
async def test_global_deadline_is_enforced() -> None:
    class SlowResponses:
        async def create(self, **_: Any) -> FakeResponse:
            await asyncio.sleep(1)
            return FakeResponse([], answer_json("太晚"))

    with pytest.raises(ResearchTimeout):
        await run_research(
            SlowResponses(),
            FakeMcpClient(),
            "問題",
            tools=TOOLS,
            deadline_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_context_budget_is_enforced_before_request() -> None:
    with pytest.raises(ContextBudgetExceeded):
        await run_research(
            FakeResponses([FakeResponse([], "不應執行")]),
            FakeMcpClient(),
            "問題",
            tools=TOOLS,
            context_budget_chars=5,
        )


@pytest.mark.asyncio
async def test_large_search_chunks_are_excerpted_before_model_context() -> None:
    responses = FakeResponses([
        FakeResponse([{
            "type": "function_call", "name": "search_filing_sections",
            "call_id": "long", "arguments": '{"query":"risk","top_k":8}',
        }]),
        FakeResponse([], answer_json("證據不足")),
    ])

    class LongChunkMcpClient:
        async def call_tool(
            self, name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            return {
                "status": "ok",
                "data": {"matches": [{"content_text": "風" * 6000}]},
                "citations": [],
            }

    await run_research(
        responses, LongChunkMcpClient(), "風險？", tools=TOOLS,
        context_budget_chars=5000,
    )

    output = next(
        item for item in responses.calls[1]["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    match = json.loads(output["output"])["result"]["data"]["matches"][0]
    assert len(match["content_text"]) == 1600
    assert match["content_truncated"] is True


@pytest.mark.asyncio
async def test_tool_limit_requests_final_answer_without_more_tools() -> None:
    class BudgetAwareResponses:
        def __init__(self) -> None:
            self.second_tool_choice: str | None = None

        async def create(self, **kwargs: Any) -> FakeResponse:
            if not any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in kwargs["input"]
            ):
                return FakeResponse([{
                    "type": "function_call", "name": "search_filing_sections",
                    "call_id": "only", "arguments": '{"query":"risk","top_k":8}',
                }])
            self.second_tool_choice = kwargs.get("tool_choice")
            if self.second_tool_choice == "none":
                return FakeResponse([], answer_json("僅根據現有證據作答"))
            return FakeResponse([{
                "type": "function_call", "name": "search_filing_sections",
                "call_id": "extra", "arguments": '{"query":"risk","top_k":8}',
            }])

    responses = BudgetAwareResponses()
    result = await run_research(
        responses, FakeMcpClient(), "風險？", tools=TOOLS, max_tool_calls=1
    )

    assert responses.second_tool_choice == "none"
    assert result.tool_calls == ("search_filing_sections",)


@pytest.mark.asyncio
async def test_composite_question_reserves_rag_then_xbrl_tools() -> None:
    class CompositeResponses:
        def __init__(self) -> None:
            self.choices: list[object] = []

        async def create(self, **kwargs: Any) -> FakeResponse:
            choice = kwargs.get("tool_choice")
            self.choices.append(choice)
            if choice == {"type": "function", "name": "search_filing_sections"}:
                return FakeResponse([{
                    "type": "function_call", "name": "search_filing_sections",
                    "call_id": "rag", "arguments": '{"query":"risk","top_k":8}',
                }])
            if choice == {"type": "function", "name": "get_financial_metric"}:
                return FakeResponse([{
                    "type": "function_call", "name": "get_financial_metric",
                    "call_id": "xbrl",
                    "arguments": '{"ticker":"NVDA","concepts":["Revenues"]}',
                }])
            return FakeResponse([], answer_json("僅能根據已取得證據說明"))

    responses = CompositeResponses()
    result = await run_research(
        responses, FakeMcpClient(),
        "NVDA 的營收與 Risk Factors 風險如何關聯？", tools=TOOLS,
    )

    assert responses.choices[:2] == [
        {"type": "function", "name": "search_filing_sections"},
        {"type": "function", "name": "get_financial_metric"},
    ]
    assert result.tool_calls == ("search_filing_sections", "get_financial_metric")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "tool_name", "arguments"),
    [
        ("AAPL 的風險為何？", "search_filing_sections", '{"query":"risk","top_k":8}'),
        (
            "MSFT 的營收為何？", "get_financial_metric",
            '{"ticker":"MSFT","concepts":["Revenues"]}',
        ),
    ],
)
async def test_single_evidence_question_starts_with_its_required_tool(
    question: str, tool_name: str, arguments: str
) -> None:
    class RouteResponses:
        def __init__(self) -> None:
            self.first_choice: object = None

        async def create(self, **kwargs: Any) -> FakeResponse:
            if not any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in kwargs["input"]
            ):
                self.first_choice = kwargs.get("tool_choice")
                if self.first_choice == {"type": "function", "name": tool_name}:
                    return FakeResponse([{
                        "type": "function_call", "name": tool_name,
                        "call_id": "evidence", "arguments": arguments,
                    }])
            return FakeResponse([], answer_json("目前證據有限"))

    responses = RouteResponses()
    result = await run_research(responses, FakeMcpClient(), question, tools=TOOLS)

    assert responses.first_choice == {"type": "function", "name": tool_name}
    assert result.tool_calls == (tool_name,)


@pytest.mark.asyncio
async def test_tool_dependency_error_is_preserved_for_ui() -> None:
    responses = FakeResponses(
        [
            FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "search_filing_sections",
                        "call_id": "dependency",
                        "arguments": '{"query":"risk","top_k":8}',
                    }
                ]
            ),
            FakeResponse([], answer_json("無法完成")),
        ]
    )

    class UnavailableMcpClient:
        async def call_tool(
            self, name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            return {
                "status": "error",
                "error": {"code": "DEPENDENCY_UNAVAILABLE"},
            }

    result = await run_research(
        responses,
        UnavailableMcpClient(),
        "風險？",
        tools=TOOLS,
    )

    assert "TOOL_ERROR:DEPENDENCY_UNAVAILABLE" in result.warnings
