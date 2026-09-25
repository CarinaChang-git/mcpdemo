"""受 SEC host allowlist、限流與重試政策保護的 HTTP client。"""

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


ALLOWED_SEC_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "cookie", "set-cookie"}
)
ACCESSION_PATTERN = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
PRIMARY_DOCUMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SecClientError(RuntimeError):
    """不含 response body 或敏感 header 的 SEC 邊界錯誤。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class Company:
    cik: str
    ticker: str
    company_name: str
    exchange: str | None = None


def normalize_cik(value: str | int) -> str:
    """將 CIK 正規化為保留前導零的 10 位字串。"""
    if isinstance(value, bool):
        raise ValueError("CIK 必須是數字字串或整數")
    raw = str(value).strip()
    if not raw.isdigit() or not 1 <= len(raw) <= 10:
        raise ValueError("CIK 必須包含 1 至 10 位數字")
    return raw.zfill(10)


def normalize_accession(value: str) -> str:
    normalized = value.strip()
    if not ACCESSION_PATTERN.fullmatch(normalized):
        raise ValueError("accession number 格式無效")
    return normalized


def validate_sec_url(url: str) -> str:
    """只允許兩個 SEC HTTPS 主機，拒絕認證資訊與非預設連接埠。"""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_SEC_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise SecClientError("SEC_URL_NOT_ALLOWED", "只允許 SEC 官方 HTTPS 主機")
    return url


def build_submissions_url(cik: str | int) -> str:
    return validate_sec_url(
        f"https://data.sec.gov/submissions/CIK{normalize_cik(cik)}.json"
    )


def build_company_facts_url(cik: str | int) -> str:
    return validate_sec_url(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{normalize_cik(cik)}.json"
    )


def build_filing_url(
    cik: str | int,
    accession_number: str,
    primary_document: str,
) -> str:
    normalized_cik = normalize_cik(cik)
    accession = normalize_accession(accession_number)
    if not PRIMARY_DOCUMENT_PATTERN.fullmatch(primary_document):
        raise ValueError("primary document 名稱無效")
    cik_without_padding = str(int(normalized_cik))
    accession_without_dashes = accession.replace("-", "")
    return validate_sec_url(
        "https://www.sec.gov/Archives/edgar/data/"
        f"{cik_without_padding}/{accession_without_dashes}/{primary_document}"
    )


def resolve_company(mapping: Mapping[str, Any], ticker: str) -> Company:
    """由 SEC ticker mapping 解析唯一公司；零筆或多筆皆拒絕。"""
    normalized_ticker = ticker.strip().upper()
    matches = [
        row
        for row in mapping.values()
        if isinstance(row, Mapping)
        and str(row.get("ticker", "")).strip().upper() == normalized_ticker
    ]
    if not matches:
        raise SecClientError("TICKER_NOT_FOUND", "ticker 不存在於 SEC mapping")
    if len(matches) != 1:
        raise SecClientError("TICKER_NOT_UNIQUE", "ticker 無法唯一對應 CIK")
    row = matches[0]
    try:
        return Company(
            cik=normalize_cik(row["cik_str"]),
            ticker=normalized_ticker,
            company_name=str(row["title"]).strip(),
            exchange=(str(row["exchange"]).strip() if row.get("exchange") else None),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SecClientError("SEC_SCHEMA_INVALID", "ticker mapping schema 無效") from error


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: "[REDACTED]" if name.lower() in SENSITIVE_HEADERS else value
        for name, value in headers.items()
    }


class _TokenBucket:
    def __init__(
        self,
        rate: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        if not 0 < rate < 10:
            raise ValueError("SEC 請求速率必須大於 0 且小於每秒 10 次")
        self.rate = rate
        self.capacity = rate
        self.tokens = rate
        self.updated_at = time.monotonic()
        self.sleep = sleep
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self.updated_at) * self.rate,
                )
                self.updated_at = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await self.sleep((1 - self.tokens) / self.rate)


class SecClient:
    """只公開由 SEC identity 組成的資料取得方法。"""

    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_second: float = 5,
        max_retries: int = 2,
        max_json_bytes: int = 10 * 1024 * 1024,
        max_html_bytes: int = 50 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.25),
    ) -> None:
        if " " not in user_agent or "@" not in user_agent:
            raise ValueError("SEC User-Agent 必須包含產品名稱與聯絡 email")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("SEC max_retries 必須介於 0 與 5")
        if max_json_bytes <= 0 or max_html_bytes <= 0:
            raise ValueError("SEC response 大小上限必須大於 0")
        self.max_retries = max_retries
        self.max_json_bytes = max_json_bytes
        self.max_html_bytes = max_html_bytes
        self.sleep = sleep
        self.jitter = jitter
        self.limiter = _TokenBucket(requests_per_second, sleep)
        self.http = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=False,
            timeout=httpx.Timeout(60, connect=5, read=30),
            transport=transport,
        )

    async def __aenter__(self) -> "SecClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def fetch_company_mapping(self) -> dict[str, Any]:
        data = await self._request_json("https://www.sec.gov/files/company_tickers.json")
        if not isinstance(data, dict):
            raise SecClientError("SEC_SCHEMA_INVALID", "ticker mapping 必須是 object")
        return data

    async def fetch_submissions(self, cik: str | int) -> dict[str, Any]:
        expected_cik = normalize_cik(cik)
        data = await self._request_json(build_submissions_url(expected_cik))
        return _validate_submissions(data, expected_cik)

    async def fetch_company_facts(self, cik: str | int) -> dict[str, Any]:
        expected_cik = normalize_cik(cik)
        data = await self._request_json(build_company_facts_url(expected_cik))
        if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
            raise SecClientError("SEC_SCHEMA_INVALID", "companyfacts schema 無效")
        try:
            actual_cik = normalize_cik(data["cik"])
        except (KeyError, TypeError, ValueError) as error:
            raise SecClientError("SEC_SCHEMA_INVALID", "companyfacts CIK 無效") from error
        if actual_cik != expected_cik:
            raise SecClientError("SEC_IDENTITY_MISMATCH", "companyfacts CIK identity 不符")
        return data

    async def fetch_filing_html(
        self,
        cik: str | int,
        accession_number: str,
        primary_document: str,
    ) -> bytes:
        url = build_filing_url(cik, accession_number, primary_document)
        return await self._request_bytes(
            url,
            allowed_content_types=("text/html", "application/xhtml+xml"),
            max_bytes=self.max_html_bytes,
            accept="text/html,application/xhtml+xml",
        )

    async def _request_json(self, url: str) -> Any:
        body = await self._request_bytes(
            url,
            allowed_content_types=("application/json",),
            max_bytes=self.max_json_bytes,
            accept="application/json",
        )
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SecClientError("SEC_SCHEMA_INVALID", "SEC JSON 無法解析") from error

    async def _request_bytes(
        self,
        url: str,
        *,
        allowed_content_types: tuple[str, ...],
        max_bytes: int,
        accept: str,
    ) -> bytes:
        safe_url = validate_sec_url(url)
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire()
            try:
                async with self.http.stream(
                    "GET", safe_url, headers={"Accept": accept}
                ) as response:
                    if response.is_redirect:
                        raise SecClientError(
                            "SEC_REDIRECT_NOT_ALLOWED",
                            "SEC 回應 redirect，已拒絕跟隨",
                        )
                    if response.status_code in RETRYABLE_STATUS_CODES:
                        if attempt == self.max_retries:
                            code = (
                                "SEC_RATE_LIMITED"
                                if response.status_code == 429
                                else "SEC_DEPENDENCY_UNAVAILABLE"
                            )
                            raise SecClientError(code, "SEC 暫時無法使用", retryable=True)
                        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                        delay = retry_after or 2**attempt
                    elif response.is_error:
                        raise SecClientError(
                            "SEC_HTTP_ERROR",
                            f"SEC 回應 HTTP {response.status_code}",
                        )
                    else:
                        content_type = response.headers.get("Content-Type", "").lower()
                        if not any(
                            content_type.startswith(expected)
                            for expected in allowed_content_types
                        ):
                            raise SecClientError(
                                "SEC_CONTENT_TYPE_INVALID",
                                "SEC endpoint 回傳非預期內容類型",
                            )
                        return await _read_limited(response, max_bytes)
                await self.sleep(delay + self.jitter())
            except SecClientError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt == self.max_retries:
                    raise SecClientError(
                        "SEC_DEPENDENCY_UNAVAILABLE",
                        "SEC 網路連線失敗",
                        retryable=True,
                    ) from error
                await self.sleep(2**attempt + self.jitter())
        raise AssertionError("SEC retry loop 不應到達此處")


async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise SecClientError("SEC_RESPONSE_TOO_LARGE", "SEC response 超過大小上限")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise SecClientError("SEC_RESPONSE_TOO_LARGE", "SEC response 超過大小上限")
    return bytes(body)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0, min(seconds, 60))


def _validate_submissions(data: Any, expected_cik: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SecClientError("SEC_SCHEMA_INVALID", "submissions 必須是 object")
    try:
        cik = normalize_cik(data["cik"])
        tickers = data["tickers"]
        recent = data["filings"]["recent"]
        columns = [
            recent[name]
            for name in (
                "accessionNumber",
                "filingDate",
                "reportDate",
                "form",
                "primaryDocument",
            )
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise SecClientError("SEC_SCHEMA_INVALID", "submissions schema 無效") from error
    if cik != expected_cik or not isinstance(tickers, list):
        raise SecClientError("SEC_IDENTITY_MISMATCH", "submissions CIK identity 不符")
    if not all(isinstance(column, list) for column in columns):
        raise SecClientError("SEC_SCHEMA_INVALID", "submissions recent 欄位必須是陣列")
    if len({len(column) for column in columns}) != 1:
        raise SecClientError("SEC_SCHEMA_INVALID", "submissions recent 欄位長度不一致")
    return data
