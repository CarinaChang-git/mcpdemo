import pytest

from sec_research.agent import CitationValidationError, validate_answer


OFFICIAL_CITATION = {
    "citation_id": "citation-1",
    "company_name": "Apple Inc.",
    "ticker": "AAPL",
    "form": "10-K",
    "period_end": "2025-09-27",
    "filing_date": "2025-10-31",
    "accession_number": "0000320193-25-000079",
    "section_code": "ITEM_1A",
    "source_url": (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000079/aapl-20250927.htm"
    ),
}


def test_valid_answer_keeps_only_current_request_sec_citations() -> None:
    answer = validate_answer(
        {
            "answer_markdown": "供應鏈風險增加。[citation-1]",
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )

    assert answer.citation_ids == ("citation-1",)
    assert answer.citations == (OFFICIAL_CITATION,)
    assert answer.is_complete is True
    assert answer.answer_markdown.endswith("研究資訊，非投資建議。")


def test_forged_citation_or_uncited_url_fails_closed() -> None:
    forged = validate_answer(
        {
            "answer_markdown": "模型聲稱有證據。",
            "citation_ids": ["forged"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )
    outside = validate_answer(
        {
            "answer_markdown": "參考 https://example.com/report",
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )

    assert forged.is_complete is outside.is_complete is False
    assert forged.citation_ids == outside.citation_ids == ()
    assert "證據不足" in forged.answer_markdown
    assert "引用驗證失敗" in forged.warnings
    assert "來源範圍驗證失敗" in outside.warnings


def test_tool_conflict_and_insufficient_evidence_force_partial_answer() -> None:
    answer = validate_answer(
        {
            "answer_markdown": "現有證據只支持部分結論。[citation-1]",
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
        evidence_warnings=("MULTIPLE_UNITS:Revenues",),
    )

    assert answer.is_complete is False
    assert "MULTIPLE_UNITS:Revenues" in answer.warnings


def test_citation_list_without_inline_marker_fails_closed() -> None:
    answer = validate_answer(
        {
            "answer_markdown": "模型提出未逐項標註的比較結論。",
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )

    assert answer.is_complete is False
    assert answer.citation_ids == ()
    assert "引用未標註於答案" in answer.warnings


def test_uncited_conclusion_after_cited_sentence_fails_closed() -> None:
    answer = validate_answer(
        {
            "answer_markdown": "## 結論\n已取得證據。[citation-1]\n\n但風險已完全消失。",
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )

    assert answer.is_complete is False
    assert answer.citation_ids == ()
    assert "重要敘述缺少逐項引用" in answer.warnings


def test_cited_markdown_table_row_is_accepted() -> None:
    answer = validate_answer(
        {
            "answer_markdown": (
                "| 年度 | 營收 |\n"
                "|---|---:|\n"
                "| 2025 | 100 [citation-1] |"
            ),
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )

    assert answer.is_complete is True


def test_uncited_markdown_table_row_fails_closed() -> None:
    answer = validate_answer(
        {
            "answer_markdown": (
                "| 年度 | 營收 |\n"
                "|---|---:|\n"
                "| 2025 | 100 [citation-1] |\n"
                "| 2024 | 90 |"
            ),
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )

    assert answer.is_complete is False
    assert "重要敘述缺少逐項引用" in answer.warnings


def test_unlisted_inline_citation_fails_closed() -> None:
    answer = validate_answer(
        {
            "answer_markdown": "錯誤來源混入答案。[forged][citation-1]",
            "citation_ids": ["citation-1"],
            "warnings": [],
            "is_complete": True,
        },
        {"citation-1": OFFICIAL_CITATION},
        evidence_required=True,
    )

    assert answer.is_complete is False
    assert "引用驗證失敗" in answer.warnings


def test_scope_out_citation_from_tool_is_rejected() -> None:
    citation = dict(OFFICIAL_CITATION)
    citation["source_url"] = "https://example.com/forged"

    with pytest.raises(CitationValidationError, match="SEC"):
        validate_answer(
            {
                "answer_markdown": "答案",
                "citation_ids": ["citation-1"],
                "warnings": [],
                "is_complete": True,
            },
            {"citation-1": citation},
            evidence_required=True,
        )
