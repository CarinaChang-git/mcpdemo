"""固定案例的答案證據支持度評分。"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from sec_research.answer_eval import evaluate_answers


FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"


SOURCE = {
    "citation_id": "aapl-risk",
    "accession_number": "0000320193-23-000010",
    "source_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019323000010/aapl-20230930.htm",
    "section_code": "ITEM_1A",
    "excerpt": "Supply chain concentration and manufacturing disruption risk.",
}
CLAIM = "AAPL 揭露供應鏈集中與製造中斷風險。"
GOLD = {
    "cases": [{
        "id": "aapl-risk",
        "sources": [SOURCE],
        "claims": [{"text": CLAIM, "supported_by": ["aapl-risk"]}],
    }]
}
PREDICTIONS = {
    "cases": [{
        "id": "aapl-risk",
        "answer_markdown": f"{CLAIM}[aapl-risk]",
        "citation_ids": ["aapl-risk"],
        "citations": [SOURCE],
    }]
}


def test_fixed_gold_scores_reproducibly_at_full_threshold() -> None:
    first = evaluate_answers(GOLD, PREDICTIONS)
    second = evaluate_answers(GOLD, PREDICTIONS)

    assert first == second
    assert first["passed"] is True
    assert first["thresholds"] == {
        "claim_support_rate": 1.0,
        "required_claim_coverage": 1.0,
        "citation_accuracy": 1.0,
    }
    assert first["scores"] == first["thresholds"]
    first["thresholds"]["claim_support_rate"] = 0.0
    assert evaluate_answers(GOLD, PREDICTIONS)["thresholds"]["claim_support_rate"] == 1.0
    assert first["counts"] == {
        "cases": 1,
        "expected_claims": 1,
        "observed_claims": 1,
        "supported_claims": 1,
        "valid_citations": 1,
        "used_citations": 1,
    }


def test_wrong_claim_or_extra_claim_fails_support_threshold() -> None:
    wrong = deepcopy(PREDICTIONS)
    wrong["cases"][0]["answer_markdown"] = "AAPL 揭露風險已消失。[aapl-risk]"
    extra = deepcopy(PREDICTIONS)
    extra["cases"][0]["answer_markdown"] += "\nAAPL 營收增加。[aapl-risk]"

    for candidate in (wrong, extra):
        result = evaluate_answers(GOLD, candidate)
        assert result["passed"] is False
        assert result["scores"]["claim_support_rate"] < 1.0
        assert result["failures"]


def test_missing_claim_cannot_pass_with_empty_answer() -> None:
    candidate = deepcopy(PREDICTIONS)
    candidate["cases"][0]["answer_markdown"] = "研究資訊，非投資建議。"
    candidate["cases"][0]["citation_ids"] = []
    candidate["cases"][0]["citations"] = []

    result = evaluate_answers(GOLD, candidate)

    assert result["passed"] is False
    assert result["scores"]["required_claim_coverage"] == 0.0


def test_wrong_or_altered_source_cannot_support_claim() -> None:
    altered = deepcopy(PREDICTIONS)
    altered["cases"][0]["citations"][0]["excerpt"] = "Other filing text."
    wrong_id = deepcopy(PREDICTIONS)
    wrong_id["cases"][0]["answer_markdown"] = f"{CLAIM}[forged]"
    wrong_id["cases"][0]["citation_ids"] = ["forged"]

    for candidate in (altered, wrong_id):
        result = evaluate_answers(GOLD, candidate)
        assert result["passed"] is False
        assert result["scores"]["citation_accuracy"] < 1.0


def test_additional_safe_citation_metadata_does_not_invalidate_source() -> None:
    candidate = deepcopy(PREDICTIONS)
    candidate["cases"][0]["citations"][0]["ticker"] = "AAPL"

    assert evaluate_answers(GOLD, candidate)["passed"] is True


def test_joint_claim_requires_both_sources() -> None:
    gold = deepcopy(GOLD)
    gold["cases"][0]["sources"].append({
        **SOURCE,
        "citation_id": "aapl-management",
        "section_code": "ITEM_7",
        "excerpt": "Services revenue and operating margin trend in fiscal 2023.",
    })
    gold["cases"][0]["claims"][0]["supported_by"] = ["aapl-risk", "aapl-management"]

    assert evaluate_answers(gold, PREDICTIONS)["passed"] is False


def test_missing_case_or_duplicate_claim_fails() -> None:
    missing = {"cases": []}
    duplicate = deepcopy(PREDICTIONS)
    duplicate["cases"][0]["answer_markdown"] += f"\n{CLAIM}[aapl-risk]"

    assert evaluate_answers(GOLD, missing)["passed"] is False
    assert evaluate_answers(GOLD, duplicate)["passed"] is False


def test_fixed_narrative_numeric_composite_and_cross_period_cases(tmp_path: Path) -> None:
    gold_path = FIXTURES / "answer_support_gold.json"
    answers_path = FIXTURES / "answer_support_answers.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    answers = json.loads(answers_path.read_text(encoding="utf-8"))

    result = evaluate_answers(gold, answers)
    assert result == evaluate_answers(gold, answers)
    assert result["passed"] is True
    assert result["counts"] == {
        "cases": 4,
        "expected_claims": 4,
        "observed_claims": 4,
        "supported_claims": 4,
        "valid_citations": 6,
        "used_citations": 6,
    }

    command = [sys.executable, "-m", "sec_research.answer_eval", str(gold_path), str(answers_path)]
    passed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert passed.returncode == 0
    assert json.loads(passed.stdout) == result

    answers["cases"][2]["answer_markdown"] = "AAPL 已無供應鏈風險。[aapl-risk-2023][aapl-revenue-2023]"
    broken_path = tmp_path / "broken_answers.json"
    broken_path.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    failed = subprocess.run([*command[:-1], str(broken_path)], capture_output=True, text=True, check=False)
    assert failed.returncode == 1
    assert json.loads(failed.stdout)["passed"] is False


def test_duplicate_gold_claim_is_rejected() -> None:
    gold = deepcopy(GOLD)
    gold["cases"][0]["claims"].append(deepcopy(gold["cases"][0]["claims"][0]))

    with pytest.raises(ValueError, match="標準主張"):
        evaluate_answers(gold, PREDICTIONS)


def test_duplicate_source_or_answer_citation_id_is_rejected() -> None:
    gold = deepcopy(GOLD)
    gold["cases"][0]["sources"].append(deepcopy(SOURCE))
    with pytest.raises(ValueError, match="標準來源"):
        evaluate_answers(gold, PREDICTIONS)

    candidate = deepcopy(PREDICTIONS)
    candidate["cases"][0]["citation_ids"].append("aapl-risk")
    assert evaluate_answers(GOLD, candidate)["passed"] is False
