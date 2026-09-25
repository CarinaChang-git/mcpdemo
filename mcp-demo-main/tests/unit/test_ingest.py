import os

import pytest

from sec_research.ingest import (
    RawContentConflict,
    RawStore,
    ResearchScope,
    _normalize_company_facts,
    build_intent_hash,
    discover_filings,
)


def test_scope_and_intent_hash_are_canonical() -> None:
    first = ResearchScope.create(
        tickers=["nvda", "AAPL", "AAPL", "msft"],
        forms=["10-Q", "10-K"],
        years=5,
    )
    second = ResearchScope.create(
        tickers=["MSFT", "NVDA", "AAPL"],
        forms=["10-K", "10-Q"],
        years=5,
    )

    assert first == second
    assert first.tickers == ("AAPL", "MSFT", "NVDA")
    assert build_intent_hash(first, "pipeline-v1") == build_intent_hash(
        second, "pipeline-v1"
    )
    assert build_intent_hash(first, "pipeline-v1") != build_intent_hash(
        first, "pipeline-v2"
    )


def test_default_scope_matches_the_approved_demo() -> None:
    scope = ResearchScope.create()

    assert scope.tickers == ("AAPL", "MSFT", "NVDA")
    assert scope.forms == ("10-K", "10-Q")
    assert scope.years == 5


def test_discovery_ignores_empty_report_date_outside_corpus_forms() -> None:
    submissions = {
        "cik": 320193,
        "tickers": ["AAPL"],
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-25-000079",
                    "0000320193-25-000080",
                ],
                "filingDate": ["2025-10-31", "2025-11-01"],
                "reportDate": ["2025-09-27", ""],
                "form": ["10-K", "8-K"],
                "primaryDocument": ["aapl.htm", "event.htm"],
            }
        },
    }

    filings = discover_filings(submissions, ResearchScope.create(["AAPL"]))

    assert len(filings) == 1
    assert filings[0].form == "10-K"


def test_raw_store_is_atomic_and_idempotent(tmp_path: pytest.TempPathFactory) -> None:
    store = RawStore(tmp_path)  # type: ignore[arg-type]
    content = b"<html><body>SEC filing</body></html>"

    created = store.save("320193", "0000320193-25-000079", content)
    repeated = store.save("0000320193", "0000320193-25-000079", content)

    assert created.created is True
    assert repeated.created is False
    assert repeated.sha256 == created.sha256
    assert repeated.path.read_bytes() == content
    with pytest.raises(RawContentConflict):
        store.save("320193", "0000320193-25-000079", b"different")


def test_raw_store_keeps_issuer_cik_when_accession_prefix_differs(
    tmp_path: pytest.TempPathFactory,
) -> None:
    stored = RawStore(tmp_path).save(
        "789019", "0001193125-26-323660", b"<html>MSFT filing</html>"
    )

    assert stored.path == (
        tmp_path / "0000789019" / "0001193125-26-323660" / "filing.html"
    )


def test_company_facts_skip_incomplete_fiscal_period_without_losing_valid_fact() -> None:
    base = {
        "accn": "0000320193-25-000079",
        "val": 100,
        "end": "2025-09-27",
        "fp": "FY",
        "form": "10-K",
        "filed": "2025-10-31",
    }
    payload = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {"USD": [{**base, "fy": None}, {**base, "fy": 2025}]}
                }
            }
        }
    }

    facts, duplicates, skipped, warnings = _normalize_company_facts(
        payload, "0000320193"
    )

    assert len(facts) == 1
    assert facts[0].fiscal_year == 2025
    assert duplicates == 0
    assert skipped == 1
    assert "INCOMPLETE_FACT:us-gaap:Revenues" in warnings


def test_raw_store_leaves_no_partial_file_when_replace_fails(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RawStore(tmp_path)  # type: ignore[arg-type]

    def fail_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated"):
        store.save("320193", "0000320193-25-000080", b"content")

    target_dir = tmp_path / "0000320193" / "0000320193-25-000080"  # type: ignore[operator]
    assert not (target_dir / "filing.html").exists()
    assert list(target_dir.glob(".tmp-*")) == []
