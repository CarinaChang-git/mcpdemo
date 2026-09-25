import json
import os
from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from mcp import Client

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
        return {
            "cik": cik,
            "name": "測試公司",
            "tickers": ["AAPL"],
            "filings": {
                "recent": {
                    "accessionNumber": [],
                    "filingDate": [],
                    "reportDate": [],
                    "form": [],
                    "primaryDocument": [],
                }
            },
        }


@pytest.mark.asyncio
async def test_mcp_discovery_lists_six_tools_and_three_resources() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    server = create_mcp_server(
        TEST_DATABASE_URL,
        FakeSecClient(),
        FakeEmbeddings(),
    )

    async with Client(server) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        status = await client.call_tool("get_corpus_status", {})

    assert {tool.name for tool in tools.tools} == {
        "list_filings",
        "get_corpus_status",
        "get_latest_filings",
        "search_filing_sections",
        "read_filing_section",
        "get_financial_metric",
    }
    assert {str(resource.uri) for resource in resources.resources} == {
        "sec://corpus/status"
    }
    assert {template.uri_template for template in templates.resource_templates} == {
        "sec://filings/{accession_number}",
        "sec://filings/{accession_number}/sections/{section_code}",
    }
    assert len(resources.resources) + len(templates.resource_templates) == 3
    assert status.is_error is not True
    assert status.structured_content is not None
    assert status.structured_content["status"] == "ok"
    assert json.loads(status.content[0].text)["status"] == "ok"


def test_streamable_http_app_exposes_mcp_and_health_routes() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    server = create_mcp_server(
        TEST_DATABASE_URL,
        FakeSecClient(),
        FakeEmbeddings(),
    )
    app = server.streamable_http_app()

    assert {getattr(route, "path", None) for route in app.routes} >= {
        "/mcp",
        "/health/live",
        "/health/ready",
    }
