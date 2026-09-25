import json
import logging
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, Field

from sec_research.mcp_server import (
    execute_tool,
    make_envelope,
    make_error,
)
from sec_research.rag import IndexUnavailableError
from sec_research.sec_client import SecClientError


class PositiveInput(BaseModel):
    value: int = Field(gt=0)


def test_success_partial_and_not_found_share_one_envelope() -> None:
    success = make_envelope(data={"count": 1})
    partial = make_envelope(
        status="partial",
        data={"count": 1},
        warnings=["部分資料缺漏"],
        citations=[{"citation_id": "abc"}],
        page={"next_cursor": "opaque"},
    )
    not_found = make_envelope(status="not_found", data={"items": []})

    for response, status in (
        (success, "ok"),
        (partial, "partial"),
        (not_found, "not_found"),
    ):
        assert response["status"] == status
        UUID(response["request_id"])
        assert "data" in response
        assert isinstance(response["citations"], list)
        assert isinstance(response["warnings"], list)
    assert "page" not in success
    assert partial["page"] == {"next_cursor": "opaque"}


@pytest.mark.parametrize(
    ("code", "retryable", "has_retry_hint"),
    [
        ("INVALID_ARGUMENT", False, False),
        ("DEPENDENCY_UNAVAILABLE", True, False),
        ("RATE_LIMITED", True, True),
        ("INDEX_UNAVAILABLE", True, False),
        ("INTERNAL_ERROR", False, False),
    ],
)
def test_structured_errors_have_stable_retry_semantics(
    code: str,
    retryable: bool,
    has_retry_hint: bool,
) -> None:
    response = make_error(code)

    assert response["status"] == "error"
    UUID(response["request_id"])
    assert response["error"]["code"] == code
    assert response["error"]["retryable"] is retryable
    assert ("retry_after_seconds" in response["error"]) is has_retry_hint


@pytest.mark.asyncio
async def test_boundary_validates_input_and_maps_dependency_errors() -> None:
    calls = 0

    async def handler(_: PositiveInput) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise AssertionError("非法輸入時不應執行 handler")

    invalid = await execute_tool(PositiveInput, {"value": 0}, handler)
    assert calls == 0
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"

    async def unavailable(_: PositiveInput) -> dict[str, Any]:
        raise SecClientError(
            "SEC_DEPENDENCY_UNAVAILABLE",
            "SEC 暫時無法使用",
            retryable=True,
        )

    async def limited(_: PositiveInput) -> dict[str, Any]:
        raise SecClientError("SEC_RATE_LIMITED", "SEC 暫時無法使用", retryable=True)

    async def no_index(_: PositiveInput) -> dict[str, Any]:
        raise IndexUnavailableError("沒有 active index")

    assert (
        await execute_tool(PositiveInput, {"value": 1}, unavailable)
    )["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert (
        await execute_tool(PositiveInput, {"value": 1}, limited)
    )["error"]["code"] == "RATE_LIMITED"
    assert (
        await execute_tool(PositiveInput, {"value": 1}, no_index)
    )["error"]["code"] == "INDEX_UNAVAILABLE"


@pytest.mark.asyncio
async def test_internal_error_is_generic_and_does_not_leak_sensitive_details() -> None:
    secret_error = "C:\\private\\OPENAI_API_KEY=sk-sensitive stack trace"

    async def handler(_: PositiveInput) -> dict[str, Any]:
        raise RuntimeError(secret_error)

    response = await execute_tool(PositiveInput, {"value": 1}, handler)
    serialized = repr(response)

    assert response["error"]["code"] == "INTERNAL_ERROR"
    assert "sk-sensitive" not in serialized
    assert "C:\\private" not in serialized
    assert "stack trace" not in serialized


@pytest.mark.asyncio
async def test_tool_logs_status_duration_and_safe_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(_: PositiveInput) -> dict[str, Any]:
        raise RuntimeError("sk-sensitive 不得出現在日誌")

    with caplog.at_level(logging.INFO, logger="sec_research.mcp_server"):
        response = await execute_tool(PositiveInput, {"value": 1}, handler)

    event = json.loads(caplog.records[-1].message)
    assert event["request_id"] == response["request_id"]
    assert event["operation"] == "PositiveInput"
    assert event["status"] == "error"
    assert event["error_code"] == "INTERNAL_ERROR"
    assert event["duration_ms"] >= 0
    assert "sk-sensitive" not in caplog.text
