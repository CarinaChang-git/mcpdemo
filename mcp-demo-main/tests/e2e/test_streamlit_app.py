import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from sec_research import app as app_module
from sec_research.config import Settings


ROOT = Path(__file__).parents[2]
APP = ROOT / "src" / "sec_research" / "app.py"
FIXTURE = ROOT / "tests" / "fixtures" / "sec" / "demo_responses.json"
QUESTIONS = (
    "比較 AAPL 最近五個已完成會計年度的 Risk Factors 變化。",
    "列出 MSFT 最近五個已完成會計年度的營收趨勢。",
    "NVDA 的資料中心營收成長與 Risk Factors 變化有何關聯？",
)


def start_app(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv("SEC_RESEARCH_DEMO_FIXTURE", str(FIXTURE))
    return AppTest.from_file(APP, default_timeout=10).run()


def submit(at: AppTest, question: str) -> AppTest:
    at.text_area[0].input(question).run()
    return next(button for button in at.button if button.label == "開始研究").click().run()


def test_page_shows_scope_readiness_and_three_builtin_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at = start_app(monkeypatch)

    assert not at.exception
    assert at.title[0].value == "SEC Filing Research Agent"
    assert all(any(button.label == question for button in at.button) for question in QUESTIONS)
    assert any("AAPL、MSFT、NVDA" in item.value for item in at.markdown)
    assert any(item.label == "模型依賴" for item in at.metric)
    submit_button = next(button for button in at.button if button.label == "開始研究")
    assert submit_button.disabled is False


@pytest.mark.parametrize(
    ("question", "expected_heading"),
    [
        (QUESTIONS[0], "RAG 敘事證據"),
        (QUESTIONS[1], "XBRL 數值證據"),
        (QUESTIONS[2], "代理綜合結論"),
    ],
)
def test_three_demo_questions_render_expected_evidence(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    expected_heading: str,
) -> None:
    at = submit(start_app(monkeypatch), question)

    assert not at.exception
    assert any(item.value == expected_heading for item in at.subheader)
    assert any("研究資訊" in item.value for item in at.markdown)
    assert any("工具軌跡" == item.value for item in at.subheader)
    assert at.code
    assert all(
        item.proto.url.startswith("https://www.sec.gov/")
        for item in at.get("link_button")
    )
    assert list(at.dataframe[0].value.columns) == [
        "name",
        "purpose",
        "status",
        "duration_ms",
        "result_count",
    ]
    rendered = "\n".join(item.value for item in at.markdown)
    assert "不得顯示的完整 prompt" not in rendered
    assert "不得顯示的秘密" not in rendered


def test_citations_show_filing_and_xbrl_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at = submit(start_app(monkeypatch), QUESTIONS[2])

    details = "\n".join(item.value for item in at.caption)
    assert "申報日：2025-02-26" in details
    assert "Accession：0001045810-25-000023" in details
    assert "Taxonomy：us-gaap" in details
    assert "數值：130497000000" in details
    assert "單位：USD" in details
    assert "會計年度：2025" in details
    assert "會計期間：FY" in details


def test_answer_discloses_default_corpus_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at = submit(start_app(monkeypatch), QUESTIONS[0])

    assert any(
        "未指定條件時的 Demo 語料邊界" in item.value
        and "AAPL、MSFT、NVDA｜10-K、10-Q｜2021-01-01 至 2025-12-31" in item.value
        and "[rag-aapl]" in item.value
        for item in at.markdown
    )


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("模擬 corpus 錯誤", "語料庫尚未就緒"),
        ("模擬 MCP 錯誤", "MCP Server 無法連線"),
        ("模擬模型錯誤", "研究模型暫時無法使用"),
        ("模擬 SEC live 錯誤", "SEC 即時資料暫時無法取得"),
        ("模擬證據錯誤", "證據驗證失敗"),
    ],
)
def test_major_error_states_are_distinct_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    message: str,
) -> None:
    at = submit(start_app(monkeypatch), query)

    assert not at.exception
    assert any(message in error.value for error in at.error)


def test_model_and_citation_html_are_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at = submit(start_app(monkeypatch), "HTML 安全測試")

    assert not at.exception
    assert all(not item.proto.allow_html for item in at.markdown)


def test_unhealthy_corpus_blocks_research(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["status"].update(
        {"ready": False, "mcp_status": "not_ready", "error": "CORPUS_UNAVAILABLE"}
    )
    fixture = tmp_path / "not-ready.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("SEC_RESEARCH_DEMO_FIXTURE", str(fixture))

    at = AppTest.from_file(APP, default_timeout=10).run()

    submit_button = next(button for button in at.button if button.label == "開始研究")
    assert submit_button.disabled is True
    assert any("語料庫尚未就緒" in error.value for error in at.error)


def test_missing_openrouter_key_blocks_live_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEC_RESEARCH_DEMO_FIXTURE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "sec-research-demo contact@example.com")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://sec_ingest:placeholder@localhost/sec_research"
    )

    at = AppTest.from_file(APP, default_timeout=10).run()

    assert not at.exception
    assert next(button for button in at.button if button.label == "開始研究").disabled
    assert any("必要設定不完整" in error.value for error in at.error)


def test_model_dependency_failure_blocks_research(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["status"].update(
        {
            "ready": False,
            "model_status": "not_ready",
            "error": "MODEL_UNAVAILABLE",
        }
    )
    fixture = tmp_path / "model-not-ready.json"
    fixture.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("SEC_RESEARCH_DEMO_FIXTURE", str(fixture))

    at = AppTest.from_file(APP, default_timeout=10).run()

    assert next(button for button in at.button if button.label == "開始研究").disabled
    assert any("研究模型暫時無法使用" in error.value for error in at.error)


@pytest.mark.parametrize("listed", [True, False])
def test_model_dependency_checks_openrouter_model_list(
    monkeypatch: pytest.MonkeyPatch,
    listed: bool,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
            self.models = SimpleNamespace(list=self.list_models)

        async def list_models(self) -> object:
            model = "openai/gpt-6-luna" if listed else "other-model"
            return SimpleNamespace(data=[SimpleNamespace(id=model)])

        async def close(self) -> None:
            return None

    monkeypatch.setattr(app_module, "AsyncOpenAI", FakeClient)
    settings = Settings(
        sec_user_agent="sec-research-demo contact@example.com",
        database_url="postgresql://postgres:placeholder@localhost/sec_research",
        openrouter_api_key="placeholder-router-key",
    )

    if listed:
        asyncio.run(app_module._check_model_dependency(settings))
    else:
        with pytest.raises(app_module.DemoServiceError, match="MODEL_UNAVAILABLE"):
            asyncio.run(app_module._check_model_dependency(settings))
