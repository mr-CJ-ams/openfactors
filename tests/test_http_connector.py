import random
from datetime import UTC, date, datetime, timedelta

import pytest

from openfactors.ingestion import (
    NATIONAL_LOCATION,
    CheckpointKey,
    ConnectorError,
    ErrorCategory,
    HttpConnector,
    HttpRequest,
    HttpResponse,
    MemoryCheckpoints,
    RetryPolicy,
    UpstreamTimeout,
    job_identity,
)

WHEN = datetime(2026, 10, 24, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []
        self.current = WHEN

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.current += timedelta(seconds=seconds)

    def now(self) -> datetime:
        return self.current


class Script:
    def __init__(self, items: list[HttpResponse | Exception]) -> None:
        self.items = list(items)
        self.timeouts: list[float] = []

    def __call__(self, request: HttpRequest, timeout: float) -> HttpResponse:
        self.timeouts.append(timeout)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _policy(**overrides: float | int) -> RetryPolicy:
    values: dict[str, float | int] = {
        "max_attempts": 3,
        "timeout_seconds": 5,
        "base_delay_seconds": 0.5,
        "max_delay_seconds": 8,
        "max_retry_after_seconds": 60,
        "min_interval_seconds": 0,
    }
    values.update(overrides)
    return RetryPolicy(**values)  # type: ignore[arg-type]


def _connector(script: Script, clock: FakeClock, **policy: float | int) -> HttpConnector:
    return HttpConnector(
        script,
        _policy(**policy),
        clock=clock,
        rng=random.Random(0),
    )


def test_success_does_not_move_the_checkpoint() -> None:
    clock = FakeClock()
    script = Script([HttpResponse(200, b"ok")])
    store = MemoryCheckpoints()
    key = CheckpointKey("open-meteo-era5", "ph-manila", "weather")
    result = _connector(script, clock).fetch(
        HttpRequest("GET", "https://example.test/archive?key=secret")
    )
    assert result.body == b"ok"
    assert result.attempts == 1
    assert clock.slept == []
    assert script.timeouts == [5]
    assert store.get(key) is None


def test_retry_after_is_waited_then_the_request_succeeds() -> None:
    clock = FakeClock()
    script = Script(
        [
            HttpResponse(429, b"", headers=(("Retry-After", "2"),)),
            HttpResponse(200, b"ok"),
        ]
    )
    result = _connector(script, clock).fetch(HttpRequest("GET", "https://example.test/archive"))
    assert result.attempts == 2
    assert clock.slept == [2.0]


def test_rate_limit_without_retry_after_uses_backoff_and_stops() -> None:
    clock = FakeClock()
    script = Script([HttpResponse(429, b""), HttpResponse(429, b""), HttpResponse(429, b"")])
    with pytest.raises(ConnectorError) as raised:
        _connector(script, clock).fetch(HttpRequest("GET", "https://example.test/archive"))
    assert raised.value.category is ErrorCategory.RATE_LIMITED
    assert raised.value.attempts == 3
    assert len(clock.slept) == 2
    assert 0.25 <= clock.slept[0] <= 0.5
    assert 0.5 <= clock.slept[1] <= 1.0


def test_retry_after_above_the_cap_fails_without_waiting() -> None:
    clock = FakeClock()
    script = Script([HttpResponse(429, b"", headers=(("Retry-After", "120"),))])
    with pytest.raises(ConnectorError) as raised:
        _connector(script, clock, max_retry_after_seconds=60).fetch(
            HttpRequest("GET", "https://example.test/archive")
        )
    assert raised.value.category is ErrorCategory.RATE_LIMITED
    assert clock.slept == []


def test_server_error_retries_then_client_error_stops() -> None:
    clock = FakeClock()
    script = Script([HttpResponse(503, b""), HttpResponse(404, b"missing")])
    with pytest.raises(ConnectorError) as raised:
        _connector(script, clock).fetch(HttpRequest("GET", "https://example.test/archive"))
    assert raised.value.category is ErrorCategory.CLIENT_HTTP
    assert raised.value.status_code == 404
    assert raised.value.attempts == 2
    assert len(clock.slept) == 1


def test_timeouts_are_bounded() -> None:
    clock = FakeClock()
    script = Script([UpstreamTimeout(), UpstreamTimeout(), UpstreamTimeout()])
    with pytest.raises(ConnectorError) as raised:
        _connector(script, clock).fetch(HttpRequest("GET", "https://example.test/archive"))
    assert raised.value.category is ErrorCategory.TIMEOUT
    assert raised.value.attempts == 3
    assert len(script.timeouts) == 3
    assert len(clock.slept) == 2


def test_partial_response_does_not_commit_a_checkpoint() -> None:
    clock = FakeClock()
    script = Script([HttpResponse(200, b"[]"), HttpResponse(200, b"[]"), HttpResponse(200, b"[]")])
    store = MemoryCheckpoints()
    key = CheckpointKey("ph-official-gazette", NATIONAL_LOCATION, "holiday")
    with pytest.raises(ConnectorError) as raised:
        _connector(script, clock).fetch(
            HttpRequest("GET", "https://example.test/calendar"),
            complete=lambda body: body != b"[]",
        )
    assert raised.value.category is ErrorCategory.PARTIAL_RESPONSE
    assert store.get(key) is None


def test_checkpoint_commit_is_monotonic_and_deterministic() -> None:
    store = MemoryCheckpoints()
    key = CheckpointKey("open-meteo-era5", "ph-manila", "weather")
    first = date(2018, 1, 1)
    assert store.commit(key, first) == first
    assert store.commit(key, first) == first
    assert store.commit(key, date(2018, 1, 2)) == date(2018, 1, 2)
    with pytest.raises(ConnectorError) as raised:
        store.commit(key, first)
    assert raised.value.category is ErrorCategory.CHECKPOINT_REGRESSION
    identity = job_identity(
        "open-meteo-era5",
        "ph-manila",
        "weather",
        date(2018, 1, 1),
        date(2018, 1, 2),
    )
    assert identity == job_identity(
        "open-meteo-era5",
        "ph-manila",
        "weather",
        date(2018, 1, 1),
        date(2018, 1, 2),
    )


def test_throttle_waits_between_calls() -> None:
    clock = FakeClock()
    script = Script([HttpResponse(200, b"a"), HttpResponse(200, b"b")])
    connector = _connector(script, clock, min_interval_seconds=5)
    connector.fetch(HttpRequest("GET", "https://example.test/archive"))
    connector.fetch(HttpRequest("GET", "https://example.test/archive"))
    assert clock.slept == [5.0]


def test_same_seed_produces_the_same_backoff() -> None:
    first_clock = FakeClock()
    second_clock = FakeClock()
    items = [HttpResponse(503, b""), HttpResponse(200, b"ok")]
    _connector(Script(items), first_clock).fetch(HttpRequest("GET", "https://example.test/a"))
    _connector(Script(list(items)), second_clock).fetch(
        HttpRequest("GET", "https://example.test/a")
    )
    assert first_clock.slept == second_clock.slept
