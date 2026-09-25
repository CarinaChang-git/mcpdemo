"""SEC HTML 清理、章節候選辨識與 fail-closed 邊界選擇。"""

import re
from dataclasses import dataclass
from datetime import date

from lxml import html
from lxml.html import HtmlElement


REQUIRED_SECTIONS = {
    "10-K": ("ITEM_1", "ITEM_1A", "ITEM_1C", "ITEM_7", "ITEM_8"),
    "10-Q": ("PART_I_ITEM_1", "PART_I_ITEM_2", "PART_II_ITEM_1A"),
}
ITEM_HEADING = re.compile(r"^item\s+(\d{1,2}[a-z]?)\b", re.IGNORECASE)
PART_HEADING = re.compile(r"^part\s+(i{1,2})\b", re.IGNORECASE)
BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "table"})


@dataclass(frozen=True, slots=True)
class ParsedSection:
    code: str
    title: str
    ordinal: int
    content: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ParseFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    sections: tuple[ParsedSection, ...]
    failures: tuple[ParseFailure, ...]


@dataclass(frozen=True, slots=True)
class _Block:
    text: str
    heading_like: bool


@dataclass(frozen=True, slots=True)
class _Candidate:
    code: str
    block_index: int
    title: str


def parse_filing(content: bytes, form: str, period_end: date | None = None) -> ParseResult:
    normalized_form = form.strip().upper()
    if normalized_form not in REQUIRED_SECTIONS:
        raise ValueError("只支援 10-K 與 10-Q")
    try:
        document = html.document_fromstring(content)
    except (ValueError, TypeError) as error:
        return ParseResult((), (ParseFailure("HTML_INVALID", str(error)),))
    _remove_non_research_content(document)
    blocks = _extract_blocks(document)
    candidates, boundaries = _find_candidates(blocks, normalized_form)
    required = REQUIRED_SECTIONS[normalized_form]
    if (
        normalized_form == "10-K"
        and period_end is not None
        and period_end < date(2023, 12, 15)
        and not any(candidate.code == "ITEM_1C" for candidate in candidates)
    ):
        required = tuple(code for code in required if code != "ITEM_1C")

    failures: list[ParseFailure] = []
    selected: list[_Candidate] = []
    for code in required:
        matches = [candidate for candidate in candidates if candidate.code == code]
        if not matches:
            failures.append(ParseFailure(code, f"找不到 {code} 正文章節"))
        elif len(matches) > 1:
            failures.append(ParseFailure(f"{code}_AMBIGUOUS", f"{code} 候選不唯一"))
        else:
            selected.append(matches[0])
    if failures:
        return ParseResult((), tuple(failures))
    if [candidate.block_index for candidate in selected] != sorted(
        candidate.block_index for candidate in selected
    ):
        return ParseResult(
            (),
            (ParseFailure("SECTION_ORDER_INVALID", "必備章節順序不符合 form 規範"),),
        )

    sections: list[ParsedSection] = []
    for ordinal, candidate in enumerate(selected):
        end = next(
            (index for index in boundaries if index > candidate.block_index),
            len(blocks),
        )
        body = "\n".join(
            block.text for block in blocks[candidate.block_index + 1 : end] if block.text
        ).strip()
        if len(body) < 40:
            failures.append(
                ParseFailure(
                    f"{candidate.code}_BODY_TOO_SHORT",
                    f"{candidate.code} 正文長度不足，拒絕猜測邊界",
                )
            )
            continue
        sections.append(
            ParsedSection(
                code=candidate.code,
                title=candidate.title,
                ordinal=ordinal,
                content=body,
                confidence=1.0,
            )
        )
    if failures:
        return ParseResult((), tuple(failures))
    return ParseResult(tuple(sections), ())


def _remove_non_research_content(document: HtmlElement) -> None:
    for element in list(document.iter()):
        if not isinstance(element.tag, str):
            continue
        tag = _local_tag(element)
        marker = " ".join(
            filter(None, (element.get("id", ""), element.get("class", "")))
        ).lower()
        if (
            tag in {"script", "style", "noscript", "nav", "hidden", "header"}
            or "table-of-contents" in marker
            or re.search(r"(^|[-_\s])toc($|[-_\s])", marker)
        ):
            element.drop_tree()


def _extract_blocks(document: HtmlElement) -> list[_Block]:
    blocks: list[_Block] = []
    for element in document.iter():
        if not isinstance(element.tag, str):
            continue
        tag = _local_tag(element)
        if tag not in BLOCK_TAGS or _has_table_ancestor(element):
            continue
        if tag == "div" and any(
            _local_tag(descendant) in BLOCK_TAGS
            for descendant in element.iterdescendants()
            if isinstance(descendant.tag, str)
        ):
            continue
        if tag == "table":
            text = _table_text(element)
        else:
            text = _normalize_text(element.text_content())
        if not text:
            continue
        blocks.append(_Block(text, _is_heading_like(element, tag, text)))
    return blocks


def _find_candidates(
    blocks: list[_Block],
    form: str,
) -> tuple[list[_Candidate], list[int]]:
    candidates: list[_Candidate] = []
    boundaries: list[int] = []
    current_part: str | None = None
    for index, block in enumerate(blocks):
        if not block.heading_like:
            continue
        part_match = PART_HEADING.match(block.text)
        if part_match:
            current_part = part_match.group(1).upper()
            boundaries.append(index)
            continue
        item_match = ITEM_HEADING.match(block.text)
        if not item_match:
            continue
        if len(block.text[item_match.end() :].strip(" .:-")) < 4:
            continue
        boundaries.append(index)
        item = item_match.group(1).upper()
        code = f"ITEM_{item}"
        if form == "10-Q":
            if current_part is None:
                continue
            code = f"PART_{current_part}_ITEM_{item}"
        candidates.append(_Candidate(code, index, block.text))
    return candidates, sorted(set(boundaries))


def _is_heading_like(element: HtmlElement, tag: str, text: str) -> bool:
    if len(text) > 220:
        return False
    item_match = ITEM_HEADING.match(text)
    if item_match and text[item_match.end() :].lstrip().startswith(","):
        return False
    if tag.startswith("h") and len(tag) == 2:
        return True
    if not element.xpath(".//a[@href]"):
        for pattern in (ITEM_HEADING, PART_HEADING):
            match = pattern.match(text)
            if match and len(text[match.end() :].strip(" .:-")) >= 4:
                return True
    marker = " ".join(
        filter(
            None,
            (
                element.get("class", ""),
                element.get("style", ""),
                element.get("role", ""),
            ),
        )
    ).lower()
    return (
        "bold" in marker
        or "font-weight" in marker
        or "heading" in marker
        or element.find("b") is not None
        or element.find("strong") is not None
    )


def _table_text(table: HtmlElement) -> str:
    rows: list[str] = []
    for row in table.xpath(".//tr"):
        cells = [
            _normalize_text(cell.text_content())
            for cell in row.xpath("./th|./td")
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _has_table_ancestor(element: HtmlElement) -> bool:
    return any(_local_tag(parent) == "table" for parent in element.iterancestors())


def _local_tag(element: HtmlElement) -> str:
    tag = str(element.tag).lower().rsplit("}", 1)[-1]
    return tag.rsplit(":", 1)[-1]


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())
