import os
from collections.abc import Sequence
from typing import Any

import pytest
from mcp import Client

from sec_research.agent import (
    SchemaCompatibilityError,
    build_openai_tools,
    strip_nulls,
)
from sec_research.db import apply_migrations
from sec_research.mcp_server import create_mcp_server


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 契約測試",
)


class FakeEmbeddings:
    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return [[1.0] + [0.0] * 1535 for _ in texts]


class FakeSecClient:
    async def fetch_submissions(self, cik: str) -> dict[str, Any]:
        raise AssertionError("schema discovery 不應呼叫 SEC")


@pytest.mark.asyncio
async def test_discovery_generates_six_strict_openai_function_tools() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    server = create_mcp_server(
        TEST_DATABASE_URL,
        FakeSecClient(),
        FakeEmbeddings(),
    )

    async with Client(server) as client:
        discovered = await client.list_tools()
        tools = await build_openai_tools(client)

    assert {tool["name"] for tool in tools} == {tool.name for tool in discovered.tools}
    assert len(tools) == 6
    assert all(tool["type"] == "function" and tool["strict"] is True for tool in tools)
    for tool in tools:
        _assert_strict_objects(tool["parameters"])

    original = {tool.name: tool.input_schema for tool in discovered.tools}
    converted = {tool["name"]: tool["parameters"] for tool in tools}
    assert converted["search_filing_sections"]["properties"]["top_k"]["maximum"] == 12
    assert converted["list_filings"]["properties"]["limit"]["maximum"] == 50
    forms_schema = next(
        item
        for item in converted["get_latest_filings"]["properties"]["forms"]["anyOf"]
        if item.get("type") == "array"
    )
    assert forms_schema["items"]["enum"] == [
        "10-K",
        "10-Q",
        "8-K",
    ]
    assert original["list_filings"]["properties"].keys() == converted[
        "list_filings"
    ]["properties"].keys()
    assert converted["get_corpus_status"]["properties"] == {}
    assert converted["get_corpus_status"]["required"] == []


def test_optional_fields_become_nullable_and_nulls_are_removed_before_mcp_call() -> None:
    schema = {
        "type": "object",
        "properties": {
            "required_text": {"type": "string"},
            "optional_limit": {"type": "integer", "default": 8},
            "nested": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
        "required": ["required_text"],
    }
    from sec_research.agent import make_strict_schema

    converted = make_strict_schema(schema)

    assert converted["required"] == ["required_text", "optional_limit", "nested"]
    assert set(converted["properties"]["optional_limit"]["type"]) == {
        "integer",
        "null",
    }
    assert "default" not in converted["properties"]["optional_limit"]
    assert strip_nulls(
        {
            "required_text": "ok",
            "optional_limit": None,
            "nested": {"value": None},
            "items": [1, None, {"x": None, "y": 2}],
        }
    ) == {
        "required_text": "ok",
        "nested": {},
        "items": [1, {"y": 2}],
    }


def test_unknown_or_incompatible_discovery_fails_closed() -> None:
    class Tool:
        name = "unknown_tool"
        description = "未知工具"
        input_schema = {"type": "object", "properties": {}}

    class Result:
        tools = [Tool()]

    class ClientStub:
        async def list_tools(self) -> Result:
            return Result()

    with pytest.raises(SchemaCompatibilityError, match="allowlist"):
        import asyncio

        asyncio.run(build_openai_tools(ClientStub()))


def _assert_strict_objects(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema.get("properties", {}))
    for value in schema.values():
        if isinstance(value, dict):
            _assert_strict_objects(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _assert_strict_objects(item)
