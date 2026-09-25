from sec_research.ingest import FilingMetadata
from sec_research.parser import ParsedSection
from sec_research.rag import chunk_section


def test_chunks_stay_within_section_and_preserve_overlap_and_metadata() -> None:
    filing = _filing()
    paragraphs = [
        " ".join(f"p{paragraph}-token{token}" for token in range(250))
        for paragraph in range(8)
    ]
    section = ParsedSection(
        code="ITEM_1A",
        title="Item 1A. Risk Factors",
        ordinal=1,
        content="\n".join(paragraphs),
        confidence=1,
    )

    chunks = chunk_section(filing, section)

    assert len(chunks) >= 3
    assert all(600 <= chunk.token_count <= 900 for chunk in chunks[:-1])
    assert chunks[-1].token_count <= 900
    assert all(chunk.section_code == "ITEM_1A" for chunk in chunks)
    assert all(chunk.ticker == "AAPL" for chunk in chunks)
    assert all(chunk.source_url == filing.source_url for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0].previous_chunk_id is None
    assert chunks[0].next_chunk_id == chunks[1].chunk_id
    assert chunks[-1].previous_chunk_id == chunks[-2].chunk_id
    assert chunks[-1].next_chunk_id is None

    first_body = chunks[0].content_text.split("\n", 1)[1].split()
    second_body = chunks[1].content_text.split("\n", 1)[1].split()
    assert first_body[-100:] == second_body[:100]


def test_chunk_hashes_and_ids_are_stable() -> None:
    filing = _filing()
    section = ParsedSection(
        code="ITEM_7",
        title="Item 7. Management Discussion",
        ordinal=3,
        content=" ".join(f"word-{index}" for index in range(1200)),
        confidence=1,
    )

    first = chunk_section(filing, section)
    second = chunk_section(filing, section)

    assert [chunk.content_sha256 for chunk in first] == [
        chunk.content_sha256 for chunk in second
    ]
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]


def _filing() -> FilingMetadata:
    return FilingMetadata.create(
        cik="0000320193",
        ticker="AAPL",
        company_name="Apple Inc.",
        form="10-K",
        filed_date="2024-11-01",
        period_end="2024-09-28",
        accession_number="0000320193-24-000123",
        primary_document="aapl-20240928.htm",
        source_url=(
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019324000123/aapl-20240928.htm"
        ),
    )
