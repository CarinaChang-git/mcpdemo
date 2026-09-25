import os
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from sec_research.db import apply_migrations
from sec_research.mcp_server import create_mcp_server


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 整合測試",
)


class FakeEmbeddings:
    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return [[1.0] + [0.0] * 1535 for _ in texts]


class FakeSecClient:
    async def fetch_submissions(self, cik: str) -> dict[str, Any]:
        raise AssertionError("健康檢查不應呼叫 SEC")


def test_health_liveness_and_readiness_reflect_active_index() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        connection.execute("UPDATE index_builds SET is_active = false WHERE is_active")
        build_id = uuid4()
        connection.execute(
            """
            INSERT INTO index_builds (
                index_build_id, embedding_provider, embedding_model,
                embedding_dimension, chunker_version, status, is_active
            ) VALUES (%s, 'openrouter', 'openai/text-embedding-3-small', 1536, 'v1', 'ready', true)
            """,
            (build_id,),
        )
    server = create_mcp_server(
        TEST_DATABASE_URL,
        FakeSecClient(),
        FakeEmbeddings(),
    )

    with TestClient(server.streamable_http_app()) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute(
                "UPDATE index_builds SET is_active = false WHERE index_build_id = %s",
                (build_id,),
            )
        not_ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert not_ready.status_code == 503
    assert not_ready.json() == {"status": "not_ready", "code": "INDEX_UNAVAILABLE"}
