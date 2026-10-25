"""One local daily attempt. The scheduler is not an HTTP route."""

import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import UTC, date, datetime

import pytest
from contracts.calendar import manila_today

from openfactors.api.catalog import catalog_server
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.http_client import Clock, RetryPolicy
from openfactors.ingestion.runs import MemoryRunLog, RunRecord
from openfactors.orchestration.daily import STEP_ORDER, main, run_daily

NOW = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)
DAY = date(2026, 10, 25)


class RecordingClock(Clock):
    def __init__(self) -> None:
        self.waits: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)


def test_a_scheduled_run_publishes_only_after_the_earlier_steps() -> None:
    calls: list[str] = []
    runs = MemoryRunLog()
    record = run_daily(_steps(calls), runs=runs, alerts=[], now=NOW)
    assert calls == list(STEP_ORDER)
    assert record.status == "succeeded"
    assert record.interval_start == record.interval_end == DAY == manila_today(NOW)
    assert record.started_at == record.finished_at == NOW
    assert record.error_category is None
    again = run_daily(_steps(calls), runs=runs, alerts=[], now=NOW)
    assert again.run_id == record.run_id
    assert calls == list(STEP_ORDER)
    assert len(runs.runs) == 1


def test_a_failed_step_does_not_publish_and_leaves_a_log_line(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="openfactors.orchestration")
    calls: list[str] = []
    alerts: list = []

    def validate(_day: date) -> None:
        calls.append("validate")
        raise ConnectorError(ErrorCategory.SCHEMA, "secret-query")

    steps = _steps(calls)
    steps["validate"] = validate
    record = run_daily(steps, runs=MemoryRunLog(), alerts=alerts, now=NOW)
    assert calls == ["weather_fetch", "holiday_fetch", "transform", "validate"]
    assert record.status == "failed"
    assert record.error_category == "schema"
    assert record.finished_at == NOW
    assert len(alerts) == 1
    assert alerts[0].run_id == record.run_id
    assert alerts[0].manila_date == DAY
    assert alerts[0].step == "validate"
    assert alerts[0].category == "schema"
    assert f"run_id={record.run_id}" in caplog.text
    assert "step=validate" in caplog.text
    assert "category=schema" in caplog.text
    assert "secret-query" not in caplog.text


def test_transient_failures_retry_until_the_connector_budget(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="openfactors.orchestration")
    policy = RetryPolicy()
    assert policy.max_attempts == 4
    clock = RecordingClock()
    calls: list[str] = []
    attempts = {"weather": 0}

    def weather(_day: date) -> None:
        attempts["weather"] += 1
        calls.append("weather_fetch")
        if attempts["weather"] < 3:
            raise ConnectorError(ErrorCategory.TIMEOUT, "secret-query")

    steps = _steps(calls)
    steps["weather_fetch"] = weather
    record = run_daily(
        steps,
        runs=MemoryRunLog(),
        alerts=[],
        now=NOW,
        policy=policy,
        clock=clock,
    )
    assert attempts["weather"] == 3
    assert clock.waits == [0.5, 1.0]
    assert record.status == "succeeded"
    assert calls.count("publish") == 1
    assert "secret-query" not in caplog.text


def test_the_retry_budget_stops_and_records_one_failure(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="openfactors.orchestration")
    clock = RecordingClock()
    alerts: list = []
    attempts = {"weather": 0}

    def weather(_day: date) -> None:
        attempts["weather"] += 1
        raise ConnectorError(ErrorCategory.TIMEOUT, "secret-query")

    calls: list[str] = []
    steps = _steps(calls)
    steps["weather_fetch"] = weather
    record = run_daily(
        steps,
        runs=MemoryRunLog(),
        alerts=alerts,
        now=NOW,
        clock=clock,
    )
    assert attempts["weather"] == 4
    assert clock.waits == [0.5, 1.0, 2.0]
    assert calls == []
    assert record.status == "failed"
    assert record.error_category == "timeout"
    assert len(alerts) == 1
    assert alerts[0].step == "weather_fetch"
    assert "category=timeout" in caplog.text
    assert "secret-query" not in caplog.text


def test_a_config_failure_is_not_retried() -> None:
    clock = RecordingClock()
    attempts = {"weather": 0}

    def weather(_day: date) -> None:
        attempts["weather"] += 1
        raise ConnectorError(ErrorCategory.CONFIG, "secret-query")

    record = run_daily(
        _replace("weather_fetch", weather),
        runs=MemoryRunLog(),
        alerts=[],
        now=NOW,
        clock=clock,
    )
    assert attempts["weather"] == 1
    assert clock.waits == []
    assert record.status == "failed"
    assert record.error_category == "config"


def test_an_unexpected_step_error_fails_without_the_exception_text(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="openfactors.orchestration")
    calls: list[str] = []

    def holiday(_day: date) -> None:
        calls.append("holiday_fetch")
        raise RuntimeError("secret-query")

    steps = _steps(calls)
    steps["holiday_fetch"] = holiday
    record = run_daily(steps, runs=MemoryRunLog(), alerts=[], now=NOW)
    assert calls == ["weather_fetch", "holiday_fetch"]
    assert record.error_category == "config"
    assert "secret-query" not in caplog.text
    assert "category=config" in caplog.text


def test_a_failed_day_can_be_attempted_again() -> None:
    runs = MemoryRunLog()
    calls: list[str] = []

    def validate(_day: date) -> None:
        calls.append("validate")
        raise ConnectorError(ErrorCategory.SCHEMA, "rejected")

    steps = _steps(calls)
    steps["validate"] = validate
    failed = run_daily(steps, runs=runs, alerts=[], now=NOW)
    calls.clear()
    succeeded = run_daily(_steps(calls), runs=runs, alerts=[], now=NOW)
    assert failed.status == "failed"
    assert succeeded.status == "succeeded"
    assert succeeded.run_id != failed.run_id
    assert calls == list(STEP_ORDER)
    assert len(runs.runs) == 2


def test_a_running_record_blocks_another_attempt() -> None:
    runs = MemoryRunLog()
    existing = RunRecord(
        run_id="00000000-0000-4000-8000-000000000370",
        source_id="daily-external-factors-ph",
        stream="daily",
        location_id="all",
        interval_start=DAY,
        interval_end=DAY,
        code_revision="local",
        status="pending",
    )
    runs.create(existing, now=NOW)
    runs.transition(existing.run_id, "running", now=NOW)
    calls: list[str] = []
    with pytest.raises(ConnectorError, match="already in progress"):
        run_daily(_steps(calls), runs=runs, alerts=[], now=NOW)
    assert calls == []
    assert runs.get(existing.run_id).status == "running"


def test_overlapping_attempts_do_not_run_the_pipeline_twice() -> None:
    calls: list[str] = []
    started = threading.Event()
    finish = threading.Event()

    def weather(_day: date) -> None:
        calls.append("weather_fetch")
        started.set()
        assert finish.wait(timeout=5)

    worker = threading.Thread(
        target=run_daily,
        kwargs={
            "steps": _replace("weather_fetch", weather, calls),
            "runs": MemoryRunLog(),
            "alerts": [],
            "now": NOW,
        },
    )
    worker.start()
    try:
        assert started.wait(timeout=5)
        with pytest.raises(ConnectorError, match="already in progress"):
            run_daily(_steps([]), runs=MemoryRunLog(), alerts=[], now=NOW)
    finally:
        finish.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert calls == list(STEP_ORDER)


def test_the_daily_command_rejects_a_date_interval_and_fails_closed(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="openfactors.orchestration")
    with pytest.raises(SystemExit, match="does not take arguments"):
        main(["2018-01-01", "2018-01-02"])
    assert main([]) == 1
    assert "step=weather_fetch" in caplog.text
    assert "category=config" in caplog.text
    assert "Live daily sources" not in caplog.text


def test_a_daily_run_needs_the_pipeline_and_a_timezone() -> None:
    with pytest.raises(ConnectorError, match="do not match"):
        run_daily({}, runs=MemoryRunLog(), alerts=[], now=NOW)
    with pytest.raises(ConnectorError, match="Code revision is empty"):
        run_daily(_steps([]), runs=MemoryRunLog(), alerts=[], now=NOW, code_revision=" ")
    with pytest.raises(ConnectorError, match="timezone-aware"):
        run_daily(_steps([]), runs=MemoryRunLog(), alerts=[], now=datetime(2026, 10, 25))


def test_the_scheduler_is_not_an_http_route() -> None:
    server = catalog_server(lambda: [], load_release=lambda _release_id: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/internal/daily", "/api/v1/scheduler", "/api/v1/jobs"):
            status, _headers, body = _read(server, path)
            assert status == 404
            assert body["error"]["message"] == "That path is not available."
        status, headers, page = _read(server, "/docs")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "The daily refresh is a local command." in page
        assert "It is not an HTTP route." in page
    finally:
        server.shutdown()
        server.server_close()


def _steps(calls: list[str]) -> dict:
    def mark(name: str):
        def step(_day: date) -> None:
            calls.append(name)

        return step

    return {name: mark(name) for name in STEP_ORDER}


def _replace(name: str, step, calls: list[str] | None = None) -> dict:
    steps = _steps([] if calls is None else calls)
    steps[name] = step
    return steps


def _read(server, path: str) -> tuple[int, dict[str, str], dict | str]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode("utf-8")
            if response.headers.get("Content-Type", "").startswith("application/json"):
                payload = json.loads(payload)
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8")
        if error.headers.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload)
        return error.code, dict(error.headers), payload
