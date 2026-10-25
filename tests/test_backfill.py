"""Authorized historical backfill. The command is local and not an HTTP route."""

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
from openfactors.ingestion.http_client import Clock
from openfactors.ingestion.runs import MemoryRunLog, RunRecord
from openfactors.orchestration.backfill import BackfillRequest, main, run_backfill
from openfactors.orchestration.daily import STEP_ORDER, run_daily

NOW = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)
DAY = date(2018, 1, 1)


class RecordingClock(Clock):
    def __init__(self) -> None:
        self.waits: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)


def test_a_dry_run_records_the_plan_and_does_not_publish(caplog) -> None:
    caplog.set_level(logging.INFO, logger="openfactors.orchestration")
    calls: list[str] = []
    audits: list = []
    record = run_backfill(
        _request(dry_run=True, end_date=date(2019, 1, 1)),
        {},
        runs=MemoryRunLog(),
        audits=audits,
        alerts=[],
        now=NOW,
    )
    assert calls == []
    assert record.status == "succeeded"
    assert record.stream == "backfill-dry-run"
    assert (record.interval_end - record.interval_start).days == 365
    assert audits[0].dry_run is True
    assert audits[0].operator == "maintainer"
    assert audits[0].reason == "OF-038"
    assert audits[0].run_id == record.run_id
    assert "backfill dry-run" in caplog.text
    assert "operator=maintainer" in caplog.text
    assert "secret-query" not in caplog.text


def test_a_backfill_publishes_only_after_the_earlier_steps() -> None:
    calls: list[str] = []
    audits: list = []
    runs = MemoryRunLog()
    record = run_backfill(
        _request(),
        _steps(calls),
        runs=runs,
        audits=audits,
        alerts=[],
        now=NOW,
    )
    assert calls == list(STEP_ORDER)
    assert record.status == "succeeded"
    assert record.stream == "backfill"
    assert record.interval_start == record.interval_end == DAY
    assert audits[0].dry_run is False
    assert audits[0].operator == "maintainer"
    again = run_backfill(
        _request(),
        _steps(calls),
        runs=runs,
        audits=audits,
        alerts=[],
        now=NOW,
    )
    assert again.run_id == record.run_id
    assert calls == list(STEP_ORDER)
    assert len(runs.runs) == 1
    assert len(audits) == 2


def test_a_dry_run_does_not_count_as_the_publish() -> None:
    runs = MemoryRunLog()
    calls: list[str] = []
    run_backfill(_request(dry_run=True), {}, runs=runs, audits=[], alerts=[], now=NOW)
    record = run_backfill(
        _request(),
        _steps(calls),
        runs=runs,
        audits=[],
        alerts=[],
        now=NOW,
    )
    assert calls == list(STEP_ORDER)
    assert record.stream == "backfill"


def test_a_failed_validation_keeps_the_previous_release(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="openfactors.orchestration")
    releases = ["published-1"]
    calls: list[str] = []
    alerts: list = []

    def validate(_scope) -> None:
        calls.append("validate")
        raise ConnectorError(ErrorCategory.SCHEMA, "secret-query")

    steps = _steps(calls)
    steps["validate"] = validate
    steps["publish"] = _publish(calls, releases)
    record = run_backfill(
        _request(),
        steps,
        runs=MemoryRunLog(),
        audits=[],
        alerts=alerts,
        now=NOW,
    )
    assert calls == ["weather_fetch", "holiday_fetch", "transform", "validate"]
    assert releases == ["published-1"]
    assert record.status == "failed"
    assert record.error_category == "schema"
    assert alerts[0].step == "validate"
    assert f"run_id={record.run_id}" in caplog.text
    assert "category=schema" in caplog.text
    assert "secret-query" not in caplog.text


def test_a_new_revision_publishes_again_and_keeps_the_old_release() -> None:
    releases = ["published-1"]
    calls: list[str] = []
    runs = MemoryRunLog()
    steps = _steps(calls)
    steps["publish"] = _publish(calls, releases)
    run_backfill(_request(), steps, runs=runs, audits=[], alerts=[], now=NOW)
    run_backfill(_request(), steps, runs=runs, audits=[], alerts=[], now=NOW)
    run_backfill(
        _request(code_revision="revision-2"),
        steps,
        runs=runs,
        audits=[],
        alerts=[],
        now=NOW,
    )
    assert calls.count("publish") == 2
    assert releases == ["published-1", "published-2", "published-3"]
    assert len(runs.runs) == 2


def test_transient_failures_retry_and_a_config_failure_does_not(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="openfactors.orchestration")
    clock = RecordingClock()
    attempts = {"weather": 0}

    def weather(_scope) -> None:
        attempts["weather"] += 1
        if attempts["weather"] < 3:
            raise ConnectorError(ErrorCategory.TIMEOUT, "secret-query")

    record = run_backfill(
        _request(),
        _replace("weather_fetch", weather),
        runs=MemoryRunLog(),
        audits=[],
        alerts=[],
        now=NOW,
        clock=clock,
    )
    assert record.status == "succeeded"
    assert attempts["weather"] == 3
    assert clock.waits == [0.5, 1.0]
    assert "secret-query" not in caplog.text
    clock = RecordingClock()
    attempts["weather"] = 0

    def refuse(_scope) -> None:
        attempts["weather"] += 1
        raise ConnectorError(ErrorCategory.CONFIG, "secret-query")

    failed = run_backfill(
        _request(),
        _replace("weather_fetch", refuse),
        runs=MemoryRunLog(),
        audits=[],
        alerts=[],
        now=NOW,
        clock=clock,
    )
    assert failed.error_category == "config"
    assert attempts["weather"] == 1
    assert clock.waits == []


def test_the_interval_and_the_operator_are_checked() -> None:
    with pytest.raises(ConnectorError, match="named operator"):
        run_backfill(_request(operator="anonymous"), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="named operator"):
        run_backfill(_request(operator="  "), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="needs a reason"):
        run_backfill(_request(reason=" "), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="That source is not available"):
        run_backfill(_request(source_id="weather"), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="That location is not available"):
        run_backfill(_request(location_id="PH-EXAMPLE"), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="That location is not available"):
        run_backfill(_request(location_id="PH-PILOT-001"), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="start_date is before 2018-01-01"):
        run_backfill(_request(start_date=date(2017, 12, 31)), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="after the current Asia/Manila date"):
        run_backfill(_request(end_date=date(2026, 10, 26)), _steps([]), **_blank())
    with pytest.raises(ConnectorError, match="start_date must be on or before end_date"):
        run_backfill(
            _request(start_date=date(2018, 1, 2), end_date=DAY),
            _steps([]),
            **_blank(),
        )
    with pytest.raises(ConnectorError, match="timezone-aware"):
        run_backfill(_request(), _steps([]), **_blank(now=datetime(2026, 10, 25)))
    allowed = run_backfill(
        _request(dry_run=True, end_date=manila_today(NOW), location_id="all"),
        {},
        **_blank(),
    )
    holiday = run_backfill(
        _request(dry_run=True, source_id="ph-official-gazette"),
        {},
        **_blank(),
    )
    leap = run_backfill(
        _request(dry_run=True, start_date=date(2020, 2, 29), end_date=date(2020, 2, 29)),
        {},
        **_blank(),
    )
    assert allowed.status == holiday.status == leap.status == "succeeded"
    assert leap.interval_start == date(2020, 2, 29)


def test_a_running_backfill_blocks_another_attempt() -> None:
    runs = MemoryRunLog()
    existing = RunRecord(
        run_id="00000000-0000-4000-8000-000000000380",
        source_id="open-meteo-era5",
        stream="backfill",
        location_id="ph-manila",
        interval_start=DAY,
        interval_end=DAY,
        code_revision="local",
        status="pending",
    )
    runs.create(existing, now=NOW)
    runs.transition(existing.run_id, "running", now=NOW)
    calls: list[str] = []
    with pytest.raises(ConnectorError, match="already in progress"):
        run_backfill(_request(), _steps(calls), runs=runs, audits=[], alerts=[], now=NOW)
    assert calls == []
    assert runs.get(existing.run_id).status == "running"


def test_a_backfill_blocks_the_daily_run() -> None:
    started = threading.Event()
    finish = threading.Event()

    def weather(_scope) -> None:
        started.set()
        assert finish.wait(timeout=5)

    worker = threading.Thread(
        target=run_backfill,
        kwargs={
            "request": _request(),
            "steps": _replace("weather_fetch", weather),
            "runs": MemoryRunLog(),
            "audits": [],
            "alerts": [],
            "now": NOW,
        },
    )
    worker.start()
    try:
        assert started.wait(timeout=5)
        with pytest.raises(ConnectorError, match="already in progress"):
            run_daily(_daily_steps(), runs=MemoryRunLog(), alerts=[], now=NOW)
    finally:
        finish.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_the_command_rejects_an_anonymous_request_and_fails_closed(caplog) -> None:
    caplog.set_level(logging.INFO, logger="openfactors.orchestration")
    with pytest.raises(SystemExit):
        main([])
    with pytest.raises(SystemExit, match="named operator"):
        main(_argv(operator="anonymous", dry_run=True))
    with pytest.raises(SystemExit, match="must be an ISO date"):
        main(_argv(start="01/01/2018", dry_run=True))
    assert main(_argv(dry_run=True)) == 0
    assert "backfill dry-run" in caplog.text
    assert main(_argv(dry_run=False)) == 1
    assert "Live backfill sources" not in caplog.text


def test_backfill_is_not_an_http_route() -> None:
    server = catalog_server(lambda: [], load_release=lambda _release_id: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _read(server, "/api/v1/backfill")
        assert status == 404
        assert body["error"]["message"] == "That path is not available."
        status, headers, page = _read(server, "/docs")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "Historical backfill is a local command." in page
        assert "It is not an HTTP route." in page
    finally:
        server.shutdown()
        server.server_close()


def _request(**overrides) -> BackfillRequest:
    values = {
        "source_id": "open-meteo-era5",
        "location_id": "ph-manila",
        "start_date": DAY,
        "end_date": DAY,
        "operator": "maintainer",
        "reason": "OF-038",
        "dry_run": False,
        "code_revision": "local",
    }
    values.update(overrides)
    return BackfillRequest(**values)


def _blank(now=NOW) -> dict:
    return {"runs": MemoryRunLog(), "audits": [], "alerts": [], "now": now}


def _steps(calls: list[str]) -> dict:
    def mark(name: str):
        def step(_scope) -> None:
            calls.append(name)

        return step

    return {name: mark(name) for name in STEP_ORDER}


def _replace(name: str, step) -> dict:
    steps = _steps([])
    steps[name] = step
    return steps


def _publish(calls: list[str], releases: list[str]):
    def step(_scope) -> None:
        calls.append("publish")
        releases.append(f"published-{len(releases) + 1}")

    return step


def _daily_steps() -> dict:
    def mark(name: str):
        def step(_day: date) -> None:
            return None

        mark.name = name
        return step

    return {name: mark(name) for name in STEP_ORDER}


def _argv(*, operator="maintainer", start="2018-01-01", dry_run=False) -> list[str]:
    argv = [
        "--source",
        "open-meteo-era5",
        "--location-id",
        "ph-manila",
        "--start-date",
        start,
        "--end-date",
        "2018-01-01",
        "--operator",
        operator,
        "--reason",
        "OF-038",
    ]
    if dry_run:
        argv.append("--dry-run")
    return argv


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
