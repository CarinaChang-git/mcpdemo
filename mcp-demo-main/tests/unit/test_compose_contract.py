from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_compose_only_publishes_demo_on_loopback_and_persists_data() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "127.0.0.1:${DEMO_PORT:-8501}:8501" in compose
    assert compose.count("ports:") == 1
    assert "pgvector/pgvector:pg18" in compose
    assert "postgres-data:/var/lib/postgresql" in compose
    assert "raw-data:/app/data/raw" in compose
    assert "condition: service_healthy" in compose


def test_compose_has_no_scheduler_or_public_ingestion_service() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "scheduler:" not in compose
    assert "ingestion:" not in compose
    assert "ingest:" not in compose


def test_compose_routes_demo_answers_through_openrouter() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert sum(
        line.strip().startswith("OPENROUTER_API_KEY:")
        for line in compose.splitlines()
    ) == 2
    assert "OPENAI_API_KEY:" not in compose
