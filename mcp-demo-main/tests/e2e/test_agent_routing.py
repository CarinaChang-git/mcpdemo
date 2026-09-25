from dataclasses import dataclass
import json
from typing import Any

import pytest

from sec_research.agent import run_research


TOOLS = [
    {
        "type": "function",
        "name": "search_filing_sections",
        "description": "搜尋 SEC filing 敘事章節",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_financial_metric",
        "description": "讀取 SEC XBRL 財務數值",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "enum": ["AAPL"]},
                "concepts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["ticker", "concepts"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


@dataclass
class FakeResponse:
    output: list[dict[str, Any]]
    output_text: str = ""
    id: str = "routing-response"


class RoutingResponses:
    async def create(self, **kwargs: Any) -> FakeResponse:
        question = kwargs["input"][0]["content"]
        completed = sum(
            item.get("type") == "function_call_output"
            for item in kwargs["input"]
            if isinstance(item, dict)
        )
        if "風險" in question and completed == 0:
            return FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "search_filing_sections",
                        "call_id": "rag",
                        "arguments": '{"query":"供應鏈風險"}',
                    }
                ]
            )
        if "營收" in question and completed == (1 if "風險" in question else 0):
            return FakeResponse(
                [
                    {
                        "type": "function_call",
                        "name": "get_financial_metric",
                        "call_id": "xbrl",
                        "arguments": '{"ticker":"AAPL","concepts":["Revenues"]}',
                    }
                ]
            )
        return FakeResponse(
            [],
            json.dumps(
                {
                    "answer_markdown": "完成",
                    "citation_ids": [],
                    "warnings": [],
                    "is_complete": False,
                },
                ensure_ascii=False,
            ),
        )


class RecordingMcpClient:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.names.append(name)
        return {"status": "ok", "data": arguments, "citations": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("AAPL 的供應鏈風險如何變化？", ["search_filing_sections"]),
        ("AAPL 的營收是多少？", ["get_financial_metric"]),
        (
            "AAPL 的供應鏈風險與營收趨勢有何關係？",
            ["search_filing_sections", "get_financial_metric"],
        ),
    ],
)
async def test_research_questions_follow_expected_tool_route(
    question: str,
    expected: list[str],
) -> None:
    mcp = RecordingMcpClient()

    result = await run_research(RoutingResponses(), mcp, question, tools=TOOLS)

    assert mcp.names == expected
    assert list(result.tool_calls) == expected
