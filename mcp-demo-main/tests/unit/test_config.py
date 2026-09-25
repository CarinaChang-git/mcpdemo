import pytest
from pydantic import ValidationError

from sec_research.config import Settings


def valid_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "sec_user_agent": "sec-research-demo contact@example.com",
        "database_url": "postgresql://sec_ingest:placeholder@localhost:5432/sec_research",
        "openrouter_api_key": "placeholder-router-key",
    }
    values.update(overrides)
    return Settings(**values)


def test_required_settings_must_be_present(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SEC_USER_AGENT", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    missing = {item["loc"][0] for item in error.value.errors()}
    assert {"sec_user_agent", "database_url"} <= missing


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("sec_requests_per_second", 10),
        ("rag_chunk_overlap", 600),
        ("agent_max_tool_calls", 7),
        ("openrouter_answer_model", "other-model"),
        ("openrouter_embedding_model", "other-embedding-model"),
        ("embedding_dimension", 3072),
    ],
)
def test_invalid_limits_and_fixed_model_invariants_are_rejected(
    override: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        valid_settings(**{override: value})


def test_secrets_are_masked_in_repr_and_json() -> None:
    settings = valid_settings()

    rendered = repr(settings) + settings.model_dump_json()

    assert "placeholder-router-key" not in rendered
    assert "sec_ingest:placeholder" not in rendered
    assert "**********" in rendered


def test_safe_defaults_match_the_approved_design() -> None:
    settings = valid_settings()

    assert settings.sec_requests_per_second == 5
    assert settings.mcp_host == "127.0.0.1"
    assert str(settings.mcp_url) == "http://127.0.0.1:8000/mcp"
    assert settings.openrouter_answer_model == "openai/gpt-6-luna"
    assert settings.openrouter_embedding_model == "openai/text-embedding-3-small"
    assert settings.embedding_dimension == 1536
    assert settings.rag_chunk_min_tokens == 600
    assert settings.rag_chunk_max_tokens == 900
    assert settings.rag_chunk_overlap == 100
    assert settings.rag_candidate_limit == 50
    assert settings.rag_rrf_k == 60
    assert settings.rag_top_k == 8
    assert settings.agent_max_tool_calls == 6
    assert settings.agent_max_input_chars == 4000
    assert settings.agent_context_budget_chars == 200_000


def test_settings_load_required_values_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "sec-research-demo contact@example.com")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://sec_ingest:placeholder@localhost:5432/sec_research",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-router-key")

    settings = Settings(_env_file=None)

    assert settings.sec_user_agent == "sec-research-demo contact@example.com"
    assert settings.openrouter_api_key is not None
    assert settings.openrouter_api_key.get_secret_value() == "placeholder-router-key"
