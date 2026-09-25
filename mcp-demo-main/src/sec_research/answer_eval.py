"""以人工標定的固定原子主張評估答案與 SEC 引用。"""

import argparse
import json
from pathlib import Path
import re
import unicodedata
from typing import Any

from sec_research.sec_client import SecClientError, validate_sec_url


THRESHOLDS = {
    "claim_support_rate": 1.0,
    "required_claim_coverage": 1.0,
    "citation_accuracy": 1.0,
}
CLAIM_LINE = re.compile(r"^(.*?)\s*((?:\[[A-Za-z0-9_-]+\]\s*)+)$")
MARKER = re.compile(r"\[([A-Za-z0-9_-]+)\]")
DISCLAIMER = "研究資訊，非投資建議。"


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def evaluate_answers(gold: dict[str, Any], predictions: dict[str, Any]) -> dict[str, Any]:
    """對固定案例計算逐項支持率、必要主張涵蓋率及引用正確率。"""
    cases = gold.get("cases")
    answers = predictions.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("標準案例不可為空")
    if not isinstance(answers, list):
        raise ValueError("答案檔必須包含 cases 陣列")
    case_ids = [case.get("id") for case in cases if isinstance(case, dict)]
    answer_ids = [case.get("id") for case in answers if isinstance(case, dict)]
    if len(case_ids) != len(cases) or len(set(case_ids)) != len(cases):
        raise ValueError("標準案例 ID 必須唯一")
    if len(answer_ids) != len(answers) or len(set(answer_ids)) != len(answers):
        raise ValueError("答案案例 ID 必須唯一")

    by_id = {answer["id"]: answer for answer in answers}
    failures: list[str] = []
    expected_count = observed_count = supported_count = 0
    valid_citations = used_citations = 0
    if set(by_id) != set(case_ids):
        failures.append("案例集合與標準案例不一致")

    for case in cases:
        case_id = case["id"]
        sources = {item["citation_id"]: item for item in case["sources"]}
        claims = {_normalize(item["text"]): set(item["supported_by"]) for item in case["claims"]}
        if len(sources) != len(case["sources"]):
            raise ValueError(f"{case_id}：標準來源 ID 必須唯一")
        if len(claims) != len(case["claims"]):
            raise ValueError(f"{case_id}：標準主張必須唯一")
        if not sources or not claims or any(
            not supported or not supported <= sources.keys()
            for supported in claims.values()
        ):
            raise ValueError(f"{case_id}：標準主張與來源不完整")
        for source in sources.values():
            try:
                validate_sec_url(source["source_url"])
            except (KeyError, SecClientError) as error:
                raise ValueError(f"{case_id}：標準來源不是 SEC URL") from error
            if source["accession_number"].replace("-", "") not in source["source_url"]:
                raise ValueError(f"{case_id}：標準來源與 accession 不一致")
        expected_count += len(claims)
        answer = by_id.get(case_id)
        if answer is None:
            failures.append(f"{case_id}：缺少答案")
            continue
        markdown = answer.get("answer_markdown")
        citation_ids = answer.get("citation_ids")
        citations = answer.get("citations")
        if not isinstance(markdown, str) or not isinstance(citation_ids, list) or not isinstance(citations, list):
            failures.append(f"{case_id}：答案結構無效")
            continue
        actual_sources = {
            item["citation_id"]: item
            for item in citations
            if isinstance(item, dict) and isinstance(item.get("citation_id"), str)
        }
        if len(actual_sources) != len(citations):
            failures.append(f"{case_id}：引用清單有重複或無效項目")
        seen_claims: set[str] = set()
        inline_ids: set[str] = set()
        for line in markdown.splitlines():
            line = line.strip()
            if not line or _normalize(line) == _normalize(DISCLAIMER):
                continue
            observed_count += 1
            match = CLAIM_LINE.fullmatch(line)
            if match is None:
                failures.append(f"{case_id}：主張缺少行內引用")
                continue
            statement = _normalize(match.group(1))
            markers = MARKER.findall(match.group(2))
            inline_ids.update(markers)
            used_citations += len(markers)
            valid_citations += sum(
                marker in sources
                and marker in actual_sources
                and all(actual_sources[marker].get(key) == value for key, value in sources[marker].items())
                for marker in markers
            )
            if (
                statement not in claims
                or statement in seen_claims
                or set(markers) != claims[statement]
                or not all(
                    marker in actual_sources
                    and all(actual_sources[marker].get(key) == value for key, value in sources[marker].items())
                    for marker in markers
                )
            ):
                failures.append(f"{case_id}：主張未受標定證據支持")
                continue
            seen_claims.add(statement)
            supported_count += 1
        if seen_claims != claims.keys():
            failures.append(f"{case_id}：必要主張未完整呈現")
        if (
            not all(isinstance(item, str) for item in citation_ids)
            or len(citation_ids) != len(set(citation_ids))
            or set(citation_ids) != inline_ids
            or set(actual_sources) != inline_ids
        ):
            failures.append(f"{case_id}：引用清單與正文不一致")

    scores = {
        "claim_support_rate": supported_count / observed_count if observed_count else 0.0,
        "required_claim_coverage": supported_count / expected_count,
        "citation_accuracy": valid_citations / used_citations if used_citations else 0.0,
    }
    return {
        "counts": {
            "cases": len(cases),
            "expected_claims": expected_count,
            "observed_claims": observed_count,
            "supported_claims": supported_count,
            "valid_citations": valid_citations,
            "used_citations": used_citations,
        },
        "scores": scores,
        "thresholds": dict(THRESHOLDS),
        "passed": not failures and all(scores[key] >= limit for key, limit in THRESHOLDS.items()),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="評估固定案例的答案證據支持度")
    parser.add_argument("gold", type=Path, help="人工標定的標準案例 JSON")
    parser.add_argument("answers", type=Path, help="同案例的答案 JSON")
    args = parser.parse_args(argv)
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    answers = json.loads(args.answers.read_text(encoding="utf-8"))
    result = evaluate_answers(gold, answers)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
