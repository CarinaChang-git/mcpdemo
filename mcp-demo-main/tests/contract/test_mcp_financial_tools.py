import os
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from sec_research.db import apply_migrations
from sec_research.mcp_server import get_financial_metric_tool


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="需要 TEST_DATABASE_URL 才能執行 PostgreSQL 契約測試",
)


@pytest.mark.asyncio
async def test_financial_metric_preserves_value_unit_period_and_accession() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    accession, start_date, end_date, filed_date = _seed_facts(
        include_ambiguous=False
    )

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        response = await get_financial_metric_tool(
            connection,
            {
                "ticker": "aapl",
                "concepts": ["Revenues"],
                "period_from": end_date,
                "period_to": end_date,
                "forms": ["10-K"],
                "unit": "USD",
            },
        )

    assert response["status"] == "ok"
    assert response["warnings"] == []
    assert response["data"]["facts"] == [
        {
            "taxonomy": "us-gaap",
            "concept": "Revenues",
            "value": "123.45",
            "unit": "USD",
            "start_date": start_date,
            "end_date": end_date,
            "fiscal_year": int(end_date[:4]),
            "fiscal_period": "FY",
            "form": "10-K",
            "filed_date": filed_date,
            "accession_number": accession,
            "source_url": response["data"]["facts"][0]["source_url"],
        }
    ]
    assert response["data"]["facts"][0]["source_url"].startswith(
        "https://www.sec.gov/"
    )


@pytest.mark.asyncio
async def test_multiunit_and_custom_taxonomy_are_partial_not_guessed() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)
    _seed_facts()

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        response = await get_financial_metric_tool(
            connection,
            {"ticker": "AAPL", "concepts": ["Revenues"]},
        )

    assert response["status"] == "partial"
    assert {fact["unit"] for fact in response["data"]["facts"]} == {"USD", "shares"}
    assert {fact["taxonomy"] for fact in response["data"]["facts"]} == {
        "us-gaap",
        "aapl",
    }
    assert "MULTIPLE_UNITS:Revenues" in response["warnings"]
    assert "CUSTOM_TAXONOMY:aapl:Revenues" in response["warnings"]


@pytest.mark.asyncio
async def test_financial_metric_not_found_and_invalid_input() -> None:
    assert TEST_DATABASE_URL is not None
    apply_migrations(TEST_DATABASE_URL)

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        missing = await get_financial_metric_tool(
            connection,
            {"ticker": "NVDA", "concepts": ["Assets"]},
        )
        invalid = [
            await get_financial_metric_tool(connection, payload)
            for payload in (
                {"ticker": "TSLA", "concepts": ["Revenues"]},
                {"ticker": "AAPL", "concepts": []},
                {"ticker": "AAPL", "concepts": ["UnsafeConcept"]},
                {"ticker": "AAPL", "concepts": ["Revenues"] * 6},
                {
                    "ticker": "AAPL",
                    "concepts": ["Revenues"],
                    "period_from": "2025-01-02",
                    "period_to": "2025-01-01",
                },
            )
        ]

    assert missing["status"] == "not_found"
    assert all(item["error"]["code"] == "INVALID_ARGUMENT" for item in invalid)


def _seed_facts(
    *, include_ambiguous: bool = True
) -> tuple[str, str, str, str]:
    assert TEST_DATABASE_URL is not None
    suffix = uuid4().int % 700_000 + 100_000
    accession = f"0000320193-25-{suffix:06d}"
    source_url = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        f"{accession.replace('-', '')}/aapl.htm"
    )
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        used_dates = {
            row[0]
            for row in connection.execute(
                """
                SELECT end_date
                FROM xbrl_facts
                WHERE cik = '0000320193' AND concept = 'Revenues'
                """
            )
        }
        end_date = date(1980, 12, 31)
        while end_date in used_dates:
            end_date += timedelta(days=1)
        start_date = end_date - timedelta(days=364)
        filed_date = end_date + timedelta(days=31)
        connection.execute(
            """
            INSERT INTO companies (cik, ticker, company_name)
            VALUES ('0000320193', 'AAPL', 'Apple Inc.')
            ON CONFLICT (cik) DO NOTHING
            """
        )
        connection.execute(
            """
            INSERT INTO filings (
                accession_number, cik, form, filed_date, period_end,
                primary_document, source_url, processing_status
            ) VALUES (
                %s, '0000320193', '10-K', %s, %s,
                'aapl.htm', %s, 'parsed'
            )
            """,
            (accession, filed_date, end_date, source_url),
        )
        facts = [("us-gaap", "USD", Decimal("123.45"))]
        if include_ambiguous:
            facts.extend(
                [
                    ("us-gaap", "shares", Decimal("50")),
                    ("aapl", "USD", Decimal("999")),
                ]
            )
        for taxonomy, unit, value in facts:
            connection.execute(
                """
                INSERT INTO xbrl_facts (
                    cik, accession_number, taxonomy, concept, unit, value,
                    start_date, end_date, fiscal_year, fiscal_period,
                    form, filed_date
                ) VALUES (
                    '0000320193', %s, %s, 'Revenues', %s, %s,
                    %s, %s, %s, 'FY', '10-K', %s
                )
                """,
                (
                    accession,
                    taxonomy,
                    unit,
                    value,
                    start_date,
                    end_date,
                    end_date.year,
                    filed_date,
                ),
            )
    return (
        accession,
        start_date.isoformat(),
        end_date.isoformat(),
        filed_date.isoformat(),
    )
