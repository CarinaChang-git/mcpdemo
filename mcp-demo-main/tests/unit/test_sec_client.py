import json
from pathlib import Path

import httpx
import pytest

from sec_research.sec_client import (
    SecClient,
    SecClientError,
    build_filing_url,
    build_submissions_url,
    normalize_accession,
    normalize_cik,
    redact_headers,
    resolve_company,
    validate_sec_url,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "sec" / "submissions.json"
USER_AGENT = "sec-research-demo contact@example.com"


def test_identifiers_and_urls_are_canonical() -> None:
    assert normalize_cik(320193) == "0000320193"
    assert normalize_cik("0000320193") == "0000320193"
    assert normalize_accession("0000320193-25-000079") == "0000320193-25-000079"
    assert build_submissions_url("320193") == (
        "https://data.sec.gov/submissions/CIK0000320193.json"
    )
    assert build_filing_url(
        "320193", "0000320193-25-000079", "aapl-20250628.htm"
    ) == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019325000079/aapl-20250628.htm"
    )
    assert build_filing_url(
        "789019", "0001193125-26-323660", "msft-20260630.htm"
    ) == (
        "https://www.sec.gov/Archives/edgar/data/789019/"
        "000119312526323660/msft-20260630.htm"
    )


@pytest.mark.parametrize(
    "value",
    ["", "AAPL", "-1", "12345678901", True],
)
def test_invalid_cik_is_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_cik(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/Archives/file.htm",
        "https://sec.gov/Archives/file.htm",
        "https://www.sec.gov.evil.example/Archives/file.htm",
        "https://127.0.0.1/Archives/file.htm",
    ],
)
def test_only_https_sec_hosts_are_allowed(url: str) -> None:
    with pytest.raises(SecClientError, match="SEC_URL_NOT_ALLOWED"):
        validate_sec_url(url)


def test_company_resolution_requires_one_unique_match() -> None:
    mapping = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }

    company = resolve_company(mapping, "aapl")

    assert company.cik == "0000320193"
    assert company.ticker == "AAPL"
    with pytest.raises(SecClientError, match="TICKER_NOT_UNIQUE"):
        resolve_company({**mapping, "2": mapping["0"]}, "AAPL")
    with pytest.raises(SecClientError, match="TICKER_NOT_FOUND"):
        resolve_company(mapping, "NVDA")


def test_sensitive_headers_are_redacted() -> None:
    redacted = redact_headers(
        {
            "Authorization": "Bearer private-value",
            "X-Api-Key": "private-value",
            "User-Agent": USER_AGENT,
        }
    )

    assert redacted == {
        "Authorization": "[REDACTED]",
        "X-Api-Key": "[REDACTED]",
        "User-Agent": USER_AGENT,
    }


@pytest.mark.asyncio
async def test_submissions_fixture_is_validated() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == USER_AGENT
        return httpx.Response(200, json=fixture)

    async with SecClient(
        USER_AGENT,
        transport=httpx.MockTransport(handler),
    ) as client:
        submissions = await client.fetch_submissions("320193")

    assert submissions["cik"] == "0000320193"
    assert submissions["filings"]["recent"]["form"] == ["10-Q"]


@pytest.mark.asyncio
async def test_retryable_responses_retry_but_general_4xx_does_not() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if calls == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=fixture)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with SecClient(
        USER_AGENT,
        transport=httpx.MockTransport(handler),
        sleep=record_sleep,
        jitter=lambda: 0,
    ) as client:
        await client.fetch_submissions("320193")

    assert calls == 3
    assert delays == [2, 2]

    async def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with SecClient(
        USER_AGENT,
        transport=httpx.MockTransport(not_found),
        sleep=record_sleep,
    ) as client:
        with pytest.raises(SecClientError) as error:
            await client.fetch_submissions("320193")

    assert error.value.code == "SEC_HTTP_ERROR"
    assert error.value.retryable is False


@pytest.mark.asyncio
async def test_redirect_and_invalid_content_are_rejected_without_following() -> None:
    requested: list[str] = []

    async def redirect(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async with SecClient(USER_AGENT, transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(SecClientError) as error:
            await client.fetch_submissions("320193")

    assert error.value.code == "SEC_REDIRECT_NOT_ALLOWED"
    assert len(requested) == 1

    async def html_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>not json</html>",
            headers={"Content-Type": "text/html"},
        )

    async with SecClient(
        USER_AGENT,
        transport=httpx.MockTransport(html_response),
    ) as client:
        with pytest.raises(SecClientError) as error:
            await client.fetch_submissions("320193")

    assert error.value.code == "SEC_CONTENT_TYPE_INVALID"


def test_rate_limit_cannot_reach_sec_threshold() -> None:
    with pytest.raises(ValueError):
        SecClient(USER_AGENT, requests_per_second=10)
