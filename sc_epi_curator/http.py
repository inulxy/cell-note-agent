"""Safe, cached, budget-aware HTTP client for public metadata sources."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any, Protocol

from .crawl_models import FetchReceipt, NetworkBudget, NetworkUsage
from .events import canonical_json


REDACTED_QUERY_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "email",
    "token",
    "key",
    "password",
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


class FetchPolicyError(FetchError):
    pass


class FetchBudgetExceeded(FetchError):
    pass


class FetchContentError(FetchError):
    pass


class RetryableFetchError(FetchError):
    pass


@dataclass(frozen=True)
class FetchPolicy:
    allowed_hosts: tuple[str, ...]
    user_agent: str = "CellNoteAgent/0.2"
    requests_per_second: float = 2.5
    timeout_seconds: float = 30.0
    max_response_bytes: int = 10_000_000
    retries: int = 3
    backoff_seconds: float = 0.5
    cache_only: bool = False


@dataclass(frozen=True)
class TransportResponse:
    status: int
    final_url: str
    headers: dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
        allowed_hosts: tuple[str, ...],
    ) -> TransportResponse: ...

    def head(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        allowed_hosts: tuple[str, ...],
    ) -> TransportResponse: ...

    def get_range(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
        allowed_hosts: tuple[str, ...],
    ) -> TransportResponse: ...


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalized = host.rstrip(".").lower()
    for allowed in allowed_hosts:
        allowed_normalized = allowed.rstrip(".").lower()
        if normalized == allowed_normalized or normalized.endswith(
            "." + allowed_normalized
        ):
            return True
    return False


def validate_url(url: str, allowed_hosts: tuple[str, ...]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise FetchPolicyError("only https URLs are allowed")
    if parsed.username or parsed.password:
        raise FetchPolicyError("userinfo in URLs is not allowed")
    if parsed.port not in {None, 443}:
        raise FetchPolicyError("non-standard URL ports are not allowed")
    host = parsed.hostname
    if not host:
        raise FetchPolicyError("URL host is required")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise FetchPolicyError("private or special-purpose IP addresses are blocked")
    if not _host_allowed(host, allowed_hosts):
        raise FetchPolicyError(f"host is not allowlisted: {host}")


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    redacted_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        redacted_query.append(
            (key, "[REDACTED]" if key.lower() in REDACTED_QUERY_KEYS else value)
        )
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(redacted_query),
            "",
        )
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self.allowed_hosts = allowed_hosts
        super().__init__()

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    @staticmethod
    def _opener(allowed_hosts: tuple[str, ...]) -> Any:
        return urllib.request.build_opener(_SafeRedirectHandler(allowed_hosts))

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
        allowed_hosts: tuple[str, ...],
    ) -> TransportResponse:
        validate_url(url, allowed_hosts)
        opener = self._opener(allowed_hosts)
        request = urllib.request.Request(url, headers=headers, method="GET")
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            validate_url(final_url, allowed_hosts)
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchContentError(
                    f"response exceeds max_response_bytes={max_bytes}"
                )
            return TransportResponse(
                status=int(response.status),
                final_url=final_url,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=body,
            )

    def head(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        allowed_hosts: tuple[str, ...],
    ) -> TransportResponse:
        validate_url(url, allowed_hosts)
        request = urllib.request.Request(url, headers=headers, method="HEAD")
        with self._opener(allowed_hosts).open(request, timeout=timeout) as response:
            final_url = response.geturl()
            validate_url(final_url, allowed_hosts)
            return TransportResponse(
                status=int(response.status),
                final_url=final_url,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=b"",
            )

    def get_range(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        max_bytes: int,
        allowed_hosts: tuple[str, ...],
    ) -> TransportResponse:
        validate_url(url, allowed_hosts)
        request_headers = dict(headers)
        request_headers["Range"] = "bytes=0-0"
        request = urllib.request.Request(
            url, headers=request_headers, method="GET"
        )
        with self._opener(allowed_hosts).open(request, timeout=timeout) as response:
            final_url = response.geturl()
            validate_url(final_url, allowed_hosts)
            # Some servers ignore Range. Read and retain at most one byte, then
            # close the response instead of accidentally streaming the file.
            body = response.read(min(max_bytes, 1))
            return TransportResponse(
                status=int(response.status),
                final_url=final_url,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=body,
            )


class RawResponseCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blob_dir = self.root / "blobs"
        self.request_dir = self.root / "requests"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.request_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def request_key(
        url: str,
        accept: str,
        *,
        method: str = "GET",
        request_headers: dict[str, str] | None = None,
    ) -> str:
        if method.upper() == "GET" and not request_headers:
            material = canonical_json(
                {"method": "GET", "url": url, "accept": accept}
            )
            return hashlib.sha256(material.encode("utf-8")).hexdigest()
        material = canonical_json(
            {
                "method": method.upper(),
                "url": url,
                "accept": accept,
                "request_headers": {
                    key.lower(): value
                    for key, value in sorted((request_headers or {}).items())
                },
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def load(self, request_key: str) -> tuple[dict[str, Any], bytes] | None:
        receipt_path = self.request_dir / f"{request_key}.json"
        if not receipt_path.exists():
            return None
        metadata = json.loads(receipt_path.read_text(encoding="utf-8"))
        blob_path = self.root / metadata["blob_path"]
        if not blob_path.exists():
            raise FetchContentError(f"cached blob is missing: {blob_path}")
        body = blob_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != metadata["body_sha256"]:
            raise FetchContentError(f"cached blob hash mismatch: {blob_path}")
        return metadata, body

    def store(
        self,
        request_key: str,
        *,
        requested_url: str,
        response: TransportResponse,
        fetched_at: str,
        method: str = "GET",
    ) -> tuple[dict[str, Any], bytes]:
        body_sha256 = hashlib.sha256(response.body).hexdigest()
        blob_relative = Path("blobs") / f"{body_sha256}.bin"
        blob_path = self.root / blob_relative
        if not blob_path.exists():
            blob_path.write_bytes(response.body)
        metadata = {
            "request_key": request_key,
            "method": method.upper(),
            "requested_url": redact_url(requested_url),
            "final_url": redact_url(response.final_url),
            "status": response.status,
            "content_type": response.headers.get("content-type", ""),
            "fetched_at": fetched_at,
            "body_sha256": body_sha256,
            "body_bytes": len(response.body),
            "blob_path": str(blob_relative),
            "response_headers": {
                key: response.headers[key]
                for key in (
                    "accept-ranges",
                    "content-length",
                    "content-range",
                    "etag",
                    "last-modified",
                )
                if key in response.headers
            },
        }
        receipt_path = self.request_dir / f"{request_key}.json"
        receipt_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return metadata, response.body


class HttpClient:
    def __init__(
        self,
        cache: RawResponseCache,
        policy: FetchPolicy,
        budget: NetworkBudget,
        *,
        usage: NetworkUsage | None = None,
        transport: HttpTransport | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        if policy.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.cache = cache
        self.policy = policy
        self.budget = budget
        self.usage = usage if usage is not None else NetworkUsage()
        self.transport = transport if transport is not None else UrllibTransport()
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request_at: dict[str, float] = {}
        self.receipts: list[FetchReceipt] = []

    def _throttle(self, host: str) -> None:
        interval = 1.0 / self.policy.requests_per_second
        now = self.monotonic()
        last = self._last_request_at.get(host)
        if last is not None:
            delay = interval - (now - last)
            if delay > 0:
                self.sleep(delay)
        self._last_request_at[host] = self.monotonic()

    def _check_request_budget(self) -> None:
        if self.usage.requests >= self.budget.max_requests:
            raise FetchBudgetExceeded("network request budget exceeded")

    def _check_byte_budget(self, body_bytes: int) -> None:
        if self.usage.bytes + body_bytes > self.budget.max_bytes:
            raise FetchBudgetExceeded("network byte budget exceeded")

    def get_bytes(
        self,
        url: str,
        *,
        accept: str = "*/*",
    ) -> tuple[bytes, FetchReceipt]:
        validate_url(url, self.policy.allowed_hosts)
        request_key = self.cache.request_key(url, accept)
        cached = self.cache.load(request_key)
        if cached is not None:
            metadata, body = cached
            self.usage.cache_hits += 1
            receipt = self._receipt(metadata, from_cache=True)
            self.receipts.append(receipt)
            return body, receipt
        if self.policy.cache_only:
            raise FetchError(f"cache miss in cache-only mode: {redact_url(url)}")

        host = urllib.parse.urlsplit(url).hostname or ""
        last_error: Exception | None = None
        for attempt in range(self.policy.retries + 1):
            self._check_request_budget()
            self._throttle(host)
            self.usage.requests += 1
            try:
                response = self.transport.get(
                    url,
                    headers={
                        "Accept": accept,
                        "Accept-Encoding": "identity",
                        "User-Agent": self.policy.user_agent,
                    },
                    timeout=self.policy.timeout_seconds,
                    max_bytes=self.policy.max_response_bytes,
                    allowed_hosts=self.policy.allowed_hosts,
                )
                validate_url(response.final_url, self.policy.allowed_hosts)
                if len(response.body) > self.policy.max_response_bytes:
                    raise FetchContentError(
                        "transport returned a response larger than "
                        f"max_response_bytes={self.policy.max_response_bytes}"
                    )
                if response.status in RETRYABLE_STATUS:
                    raise RetryableFetchError(
                        f"retryable HTTP status: {response.status}"
                    )
                if response.status < 200 or response.status >= 300:
                    raise FetchError(f"unexpected HTTP status: {response.status}")
                self._check_byte_budget(len(response.body))
                self.usage.bytes += len(response.body)
                fetched_at = datetime.now(timezone.utc).isoformat()
                metadata, body = self.cache.store(
                    request_key,
                    requested_url=url,
                    response=response,
                    fetched_at=fetched_at,
                )
                receipt = self._receipt(metadata, from_cache=False)
                self.receipts.append(receipt)
                return body, receipt
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in RETRYABLE_STATUS:
                    break
            except (FetchBudgetExceeded, FetchPolicyError, FetchContentError):
                raise
            except (urllib.error.URLError, TimeoutError, RetryableFetchError) as error:
                last_error = error
            except FetchError as error:
                last_error = error
                break
            if attempt < self.policy.retries:
                self.sleep(self.policy.backoff_seconds * (2**attempt))
        raise FetchError(
            f"GET failed after {self.policy.retries + 1} attempts: "
            f"{redact_url(url)}: {last_error}"
        )

    def get_text(
        self,
        url: str,
        *,
        accept: str = "text/plain, text/html;q=0.9, application/xml;q=0.8",
    ) -> tuple[str, FetchReceipt]:
        body, receipt = self.get_bytes(url, accept=accept)
        charset = "utf-8"
        content_type = receipt.content_type
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0]
        try:
            return body.decode(charset, errors="strict"), receipt
        except (LookupError, UnicodeDecodeError):
            return body.decode("utf-8", errors="replace"), receipt

    def get_json(
        self,
        url: str,
    ) -> tuple[dict[str, Any], FetchReceipt]:
        value, receipt = self.get_json_value(url)
        if not isinstance(value, dict):
            raise FetchContentError("expected a JSON object response")
        return value, receipt

    def get_json_value(
        self,
        url: str,
    ) -> tuple[Any, FetchReceipt]:
        body, receipt = self.get_bytes(
            url,
            accept="application/json, application/*+json;q=0.9",
        )
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FetchContentError(f"invalid JSON from {redact_url(url)}") from error
        return value, receipt

    def probe(
        self,
        url: str,
        *,
        accept: str = "application/octet-stream",
    ) -> FetchReceipt:
        """Probe a remote object without downloading it.

        HEAD is preferred. Servers that reject HEAD with 403, 405, or 501 are
        retried with a one-byte Range GET. A server that ignores Range still
        contributes at most one body byte to the crawl budget.
        """

        validate_url(url, self.policy.allowed_hosts)
        try:
            return self._probe_request(url, accept=accept, method="HEAD")
        except FetchError as error:
            status = getattr(error, "status", None)
            if status not in {403, 405, 501}:
                raise
        return self._probe_request(url, accept=accept, method="RANGE_GET")

    def _probe_request(
        self,
        url: str,
        *,
        accept: str,
        method: str,
    ) -> FetchReceipt:
        request_headers = {"Range": "bytes=0-0"} if method == "RANGE_GET" else {}
        request_key = self.cache.request_key(
            url,
            accept,
            method=method,
            request_headers=request_headers,
        )
        cached = self.cache.load(request_key)
        if cached is not None:
            metadata, _body = cached
            self.usage.cache_hits += 1
            receipt = self._receipt(metadata, from_cache=True)
            self.receipts.append(receipt)
            return receipt
        if self.policy.cache_only:
            raise FetchError(f"cache miss in cache-only mode: {redact_url(url)}")

        host = urllib.parse.urlsplit(url).hostname or ""
        last_error: Exception | None = None
        for attempt in range(self.policy.retries + 1):
            self._check_request_budget()
            self._throttle(host)
            self.usage.requests += 1
            try:
                common_headers = {
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "User-Agent": self.policy.user_agent,
                }
                if method == "HEAD":
                    response = self.transport.head(
                        url,
                        headers=common_headers,
                        timeout=self.policy.timeout_seconds,
                        allowed_hosts=self.policy.allowed_hosts,
                    )
                else:
                    response = self.transport.get_range(
                        url,
                        headers=common_headers,
                        timeout=self.policy.timeout_seconds,
                        max_bytes=1,
                        allowed_hosts=self.policy.allowed_hosts,
                    )
                validate_url(response.final_url, self.policy.allowed_hosts)
                if len(response.body) > 1:
                    raise FetchContentError(
                        "remote probe transport returned more than one byte"
                    )
                if response.status in RETRYABLE_STATUS:
                    raise RetryableFetchError(
                        f"retryable HTTP status: {response.status}"
                    )
                if response.status < 200 or response.status >= 300:
                    error = FetchError(
                        f"unexpected HTTP status: {response.status}"
                    )
                    setattr(error, "status", response.status)
                    raise error
                self._check_byte_budget(len(response.body))
                self.usage.bytes += len(response.body)
                fetched_at = datetime.now(timezone.utc).isoformat()
                metadata, _body = self.cache.store(
                    request_key,
                    requested_url=url,
                    response=response,
                    fetched_at=fetched_at,
                    method=method,
                )
                receipt = self._receipt(metadata, from_cache=False)
                self.receipts.append(receipt)
                return receipt
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in RETRYABLE_STATUS:
                    wrapped = FetchError(f"unexpected HTTP status: {error.code}")
                    setattr(wrapped, "status", error.code)
                    raise wrapped from error
            except (FetchBudgetExceeded, FetchPolicyError, FetchContentError):
                raise
            except (urllib.error.URLError, TimeoutError, RetryableFetchError) as error:
                last_error = error
            except FetchError:
                raise
            if attempt < self.policy.retries:
                self.sleep(self.policy.backoff_seconds * (2**attempt))
        raise FetchError(
            f"{method} failed after {self.policy.retries + 1} attempts: "
            f"{redact_url(url)}: {last_error}"
        )

    @staticmethod
    def _receipt(metadata: dict[str, Any], *, from_cache: bool) -> FetchReceipt:
        return FetchReceipt(
            request_key=metadata["request_key"],
            requested_url=metadata["requested_url"],
            final_url=metadata["final_url"],
            status=int(metadata["status"]),
            content_type=metadata["content_type"],
            fetched_at=metadata["fetched_at"],
            body_sha256=metadata["body_sha256"],
            body_bytes=int(metadata["body_bytes"]),
            blob_path=metadata["blob_path"],
            from_cache=from_cache,
            method=str(metadata.get("method", "GET")),
            response_headers=dict(metadata.get("response_headers", {})),
        )


def build_url(base: str, params: dict[str, Any]) -> str:
    query = urllib.parse.urlencode(
        [(key, str(value)) for key, value in params.items() if value is not None]
    )
    return f"{base}?{query}"
