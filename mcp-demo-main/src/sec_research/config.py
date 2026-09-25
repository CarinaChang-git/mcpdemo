"""集中驗證執行設定，避免秘密或不安全限制值進入核心流程。"""

from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """由環境變數載入且經型別驗證的應用程式設定。"""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    sec_user_agent: Annotated[str, Field(min_length=8, max_length=200)]
    sec_requests_per_second: Annotated[float, Field(gt=0, lt=10)] = 5
    sec_connect_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5
    sec_read_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30
    sec_request_deadline_seconds: Annotated[float, Field(gt=0, le=300)] = 60

    database_url: SecretStr
    database_statement_timeout_ms: Annotated[int, Field(ge=100, le=60_000)] = 10_000

    mcp_host: str = "127.0.0.1"
    mcp_port: Annotated[int, Field(ge=1, le=65_535)] = 8000
    mcp_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000/mcp")
    mcp_request_deadline_seconds: Annotated[float, Field(gt=0, le=300)] = 30

    openrouter_api_key: SecretStr | None = None
    openrouter_answer_model: Literal["openai/gpt-6-luna"] = "openai/gpt-6-luna"
    openrouter_embedding_model: Literal["openai/text-embedding-3-small"] = (
        "openai/text-embedding-3-small"
    )
    embedding_dimension: Literal[1536] = 1536

    rag_chunk_min_tokens: Annotated[int, Field(ge=100, le=2000)] = 600
    rag_chunk_max_tokens: Annotated[int, Field(ge=100, le=2000)] = 900
    rag_chunk_overlap: Annotated[int, Field(ge=0, le=500)] = 100
    rag_candidate_limit: Annotated[int, Field(ge=1, le=50)] = 50
    rag_rrf_k: Annotated[int, Field(ge=1, le=1000)] = 60
    rag_top_k: Annotated[int, Field(ge=1, le=12)] = 8

    agent_max_tool_calls: Annotated[int, Field(ge=1, le=6)] = 6
    agent_max_input_chars: Annotated[int, Field(ge=1, le=4000)] = 4000
    agent_context_budget_chars: Annotated[int, Field(ge=1000, le=200_000)] = 200_000
    agent_deadline_seconds: Annotated[float, Field(gt=0, le=600)] = 120

    @field_validator("sec_user_agent")
    @classmethod
    def validate_sec_user_agent(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if "@" not in normalized or " " not in normalized:
            raise ValueError("SEC_USER_AGENT 必須包含產品名稱與聯絡 email")
        return normalized

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL 必須使用 PostgreSQL URL")
        return value

    @model_validator(mode="after")
    def validate_rag_limits(self) -> Self:
        if self.rag_chunk_min_tokens > self.rag_chunk_max_tokens:
            raise ValueError("RAG chunk 最小 tokens 不得大於最大 tokens")
        if self.rag_chunk_overlap >= self.rag_chunk_min_tokens:
            raise ValueError("RAG chunk overlap 必須小於最小 chunk tokens")
        return self
