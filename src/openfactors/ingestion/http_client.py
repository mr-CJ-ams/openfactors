"""Shared HTTP fetch for source connectors.

Retries are bounded. A 429 waits for Retry-After when that wait fits the cap.
5xx responses, timeouts, and network errors use exponential backoff with jitter.
A successful response does not move a checkpoint.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from openfactors.ingestion.errors import ConnectorError, ErrorCategory, UpstreamTimeout


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    timeout_seconds: float = 30.0
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    max_retry_after_seconds: float = 60.0
    min_interval_seconds: float = 0.0


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    body: bytes
    headers: Mapping[str, str]
    attempts: int


class Clock:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> datetime:
        return datetime.now(UTC)


class HttpConnector:
    def __init__(
        self,
        transport: Callable[[HttpRequest, float], HttpResponse],
        policy: RetryPolicy | None = None,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._transport = transport
        self.policy = policy or RetryPolicy()
        self._clock = clock or Clock()
        self._rng = rng or random.Random()
        self._last_started: datetime | None = None
        if self.policy.max_attempts < 1:
            raise ConnectorError(ErrorCategory.CONFIG, "max_attempts must be at least 1.")
        if self.policy.timeout_seconds <= 0:
            raise ConnectorError(ErrorCategory.CONFIG, "timeout_seconds must be positive.")

    def fetch(
        self,
        request: HttpRequest,
        *,
        complete: Callable[[bytes], bool] | None = None,
    ) -> FetchResult:
        self._throttle()
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                response = self._transport(request, self.policy.timeout_seconds)
            except UpstreamTimeout as exc:
                error = ConnectorError(
                    ErrorCategory.TIMEOUT,
                    f"Upstream request timed out for {safe_url(request.url)}.",
                    attempts=attempt,
                )
                self._wait_or_raise(attempt, error, self._backoff(attempt), from_exc=exc)
                continue
            except OSError as exc:
                error = ConnectorError(
                    ErrorCategory.NETWORK,
                    "Upstream network request failed.",
                    attempts=attempt,
                )
                self._wait_or_raise(attempt, error, self._backoff(attempt), from_exc=exc)
                continue
            headers = _header_map(response.headers)
            if response.status_code == 429:
                delay = _retry_after_seconds(headers, self._clock.now())
                if delay is None:
                    delay = self._backoff(attempt)
                elif delay > self.policy.max_retry_after_seconds:
                    raise ConnectorError(
                        ErrorCategory.RATE_LIMITED,
                        "Retry-After exceeds the connector wait cap.",
                        status_code=429,
                        attempts=attempt,
                    ) from None
                error = ConnectorError(
                    ErrorCategory.RATE_LIMITED,
                    "Upstream rate limit persisted.",
                    status_code=429,
                    attempts=attempt,
                )
                self._wait_or_raise(attempt, error, delay)
                continue
            if response.status_code == 408 or 500 <= response.status_code <= 599:
                category = (
                    ErrorCategory.TIMEOUT
                    if response.status_code == 408
                    else ErrorCategory.UPSTREAM_HTTP
                )
                error = ConnectorError(
                    category,
                    f"Upstream returned HTTP {response.status_code}.",
                    status_code=response.status_code,
                    attempts=attempt,
                )
                self._wait_or_raise(attempt, error, self._backoff(attempt))
                continue
            if response.status_code >= 400:
                raise ConnectorError(
                    ErrorCategory.CLIENT_HTTP,
                    f"Upstream returned HTTP {response.status_code}.",
                    status_code=response.status_code,
                    attempts=attempt,
                )
            if complete is not None and not complete(response.body):
                error = ConnectorError(
                    ErrorCategory.PARTIAL_RESPONSE,
                    "Upstream response was incomplete.",
                    status_code=response.status_code,
                    attempts=attempt,
                )
                self._wait_or_raise(attempt, error, self._backoff(attempt))
                continue
            return FetchResult(
                status_code=response.status_code,
                body=response.body,
                headers=headers,
                attempts=attempt,
            )
        raise ConnectorError(ErrorCategory.CONFIG, "Retry loop ended without a result.")

    def _wait_or_raise(
        self,
        attempt: int,
        error: ConnectorError,
        delay: float,
        *,
        from_exc: BaseException | None = None,
    ) -> None:
        if attempt >= self.policy.max_attempts:
            raise error from from_exc
        self._clock.sleep(delay)

    def _backoff(self, attempt: int) -> float:
        raw = self.policy.base_delay_seconds * (2 ** (attempt - 1))
        capped = min(raw, self.policy.max_delay_seconds)
        # Equal jitter keeps the wait inside half of the capped delay and the cap.
        fraction = 0.5 + (self._rng.random() * 0.5)
        return capped * fraction

    def _throttle(self) -> None:
        interval = self.policy.min_interval_seconds
        now = self._clock.now()
        if interval > 0 and self._last_started is not None:
            elapsed = (now - self._last_started).total_seconds()
            if elapsed < interval:
                self._clock.sleep(interval - elapsed)
                now = self._clock.now()
        self._last_started = now


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _header_map(headers: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers}


def _retry_after_seconds(headers: Mapping[str, str], now: datetime) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    text = raw.strip()
    if text.isdigit():
        return float(text)
    parsed = parsedate_to_datetime(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - now).total_seconds())
