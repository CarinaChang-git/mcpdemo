from datetime import date
from pathlib import Path

import pytest

from sec_research.parser import parse_filing


FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"


@pytest.mark.parametrize(
    ("form", "fixture", "expected_codes"),
    [
        ("10-K", "10k.html", ["ITEM_1", "ITEM_1A", "ITEM_1C", "ITEM_7", "ITEM_8"]),
        (
            "10-Q",
            "10q.html",
            ["PART_I_ITEM_1", "PART_I_ITEM_2", "PART_II_ITEM_1A"],
        ),
    ],
)
def test_required_sections_are_extracted_in_order(
    form: str,
    fixture: str,
    expected_codes: list[str],
) -> None:
    result = parse_filing((FIXTURES / fixture).read_bytes(), form)

    assert result.failures == ()
    assert [section.code for section in result.sections] == expected_codes
    assert [section.ordinal for section in result.sections] == list(
        range(len(expected_codes))
    )
    assert all(section.confidence == 1 for section in result.sections)


def test_navigation_hidden_metadata_and_scripts_are_not_in_section_text() -> None:
    result = parse_filing((FIXTURES / "10k.html").read_bytes(), "10-K")
    combined = "\n".join(section.content for section in result.sections)

    assert "........" not in combined
    assert "secret metadata" not in combined
    assert "bad()" not in combined


def test_inline_xbrl_style_headings_without_bold_parent_are_recognized() -> None:
    source = """
    <html><body>
    <table><tr><td><a href="#risk">Item 1A.</a></td><td>Risk Factors</td></tr></table>
    <div><span>Item 1. Business</span></div>
    <p>The company describes its products and services in this business section.</p>
    <div id="risk"><span>Item 1A. Risk Factors</span></div>
    <p>The company describes material operating risks in this risk section.</p>
    <div><span>Item 1C. Cybersecurity</span></div>
    <p>The company describes cybersecurity governance and controls here.</p>
    <div><span>Item 7. Management's Discussion and Analysis</span></div>
    <p>Management discusses results, liquidity, and capital resources here.</p>
    <div><span>Item 8. Financial Statements</span></div>
    <p>The company presents audited financial statements and related notes.</p>
    </body></html>
    """

    result = parse_filing(source.encode(), "10-K")

    assert result.failures == ()
    assert [section.code for section in result.sections] == [
        "ITEM_1", "ITEM_1A", "ITEM_1C", "ITEM_7", "ITEM_8"
    ]


def test_item_reference_does_not_create_a_second_heading() -> None:
    source = (FIXTURES / "10q.html").read_text(encoding="utf-8").replace(
        "<h1>Part II — Other Information</h1>",
        "<div>Item 1, 1A</div><h1>Part II — Other Information</h1>",
    )

    result = parse_filing(source.encode(), "10-Q")

    assert result.failures == ()
    assert [section.code for section in result.sections] == [
        "PART_I_ITEM_1", "PART_I_ITEM_2", "PART_II_ITEM_1A"
    ]


def test_table_rows_keep_order_and_cells() -> None:
    result = parse_filing((FIXTURES / "10k.html").read_bytes(), "10-K")
    item_8 = next(section for section in result.sections if section.code == "ITEM_8")

    assert "Year | Revenue" in item_8.content
    assert item_8.content.index("Year | Revenue") < item_8.content.index("2024 | 100")


def test_large_table_keeps_first_and_last_row_in_order() -> None:
    source = (FIXTURES / "10k.html").read_text(encoding="utf-8")
    rows = "".join(
        f"<tr><td>Row {index:03d}</td><td>{index}</td></tr>" for index in range(200)
    )
    source = source.replace(
        "<table><tr><th>Year</th><th>Revenue</th></tr><tr><td>2024</td><td>100</td></tr></table>",
        f"<table><tr><th>Label</th><th>Value</th></tr>{rows}</table>",
    )

    result = parse_filing(source.encode(), "10-K")
    item_8 = next(section for section in result.sections if section.code == "ITEM_8")

    assert item_8.content.index("Row 000 | 0") < item_8.content.index("Row 199 | 199")


@pytest.mark.parametrize(
    ("html", "expected_code"),
    [
        (
            "<html><body><h2>Item 1. Business</h2><p>Only one sufficiently long section body is present here.</p></body></html>",
            "ITEM_1A",
        ),
        (
            """
            <html><body>
            <h2>Item 1. Business</h2><p>Business content is long enough for a reliable boundary.</p>
            <h2>Item 1A. Risk Factors</h2><p>Risk content is long enough for a reliable boundary.</p>
            <h2>Item 7. MD&amp;A</h2><p>Management discussion appears before cybersecurity incorrectly.</p>
            <h2>Item 1C. Cybersecurity</h2><p>Cybersecurity content appears after item seven incorrectly.</p>
            <h2>Item 8. Financial Statements</h2><p>Financial statements content is long enough.</p>
            </body></html>
            """,
            "SECTION_ORDER_INVALID",
        ),
    ],
)
def test_missing_or_abnormal_sections_fail_closed(html: str, expected_code: str) -> None:
    result = parse_filing(html.encode(), "10-K")

    assert result.sections == ()
    assert any(failure.code == expected_code for failure in result.failures)


def test_duplicate_heading_is_ambiguous() -> None:
    html = (FIXTURES / "10k.html").read_text(encoding="utf-8").replace(
        "<h2>Item 1A. Risk Factors</h2>",
        "<h2>Item 1A. Risk Factors</h2><p>First body is intentionally duplicated.</p>"
        "<h2>Item 1A. Risk Factors</h2>",
    )

    result = parse_filing(html.encode(), "10-K")

    assert result.sections == ()
    assert any(failure.code == "ITEM_1A_AMBIGUOUS" for failure in result.failures)


def test_item_1c_is_not_required_before_sec_effective_period() -> None:
    source = (FIXTURES / "10k.html").read_text(encoding="utf-8").replace(
        "<h2>Item 1C. Cybersecurity</h2>", "<h2>Item 2. Properties</h2>"
    )

    old = parse_filing(source.encode(), "10-K", date(2023, 12, 14))
    new = parse_filing(source.encode(), "10-K", date(2023, 12, 15))

    assert old.failures == ()
    assert [section.code for section in old.sections] == [
        "ITEM_1", "ITEM_1A", "ITEM_7", "ITEM_8"
    ]
    assert any(failure.code == "ITEM_1C" for failure in new.failures)


def test_existing_item_1c_is_preserved_before_effective_period() -> None:
    result = parse_filing(
        (FIXTURES / "10k.html").read_bytes(), "10-K", date(2023, 9, 30)
    )

    assert result.failures == ()
    assert [section.code for section in result.sections] == [
        "ITEM_1", "ITEM_1A", "ITEM_1C", "ITEM_7", "ITEM_8"
    ]


def test_short_item_page_headers_are_not_section_candidates() -> None:
    source = (FIXTURES / "10q.html").read_text(encoding="utf-8").replace(
        "<h2>Item 1. Financial Statements</h2>",
        "<h2>Item 1</h2><h2>Item 1. Financial Statements</h2>",
    )

    result = parse_filing(source.encode(), "10-Q")

    assert result.failures == ()
    assert [section.code for section in result.sections] == [
        "PART_I_ITEM_1", "PART_I_ITEM_2", "PART_II_ITEM_1A"
    ]
