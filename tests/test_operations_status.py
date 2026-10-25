"""Local operations report. Last attempt, last success, and last publication differ."""

import io
import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta

import pytest

from openfactors.api.catalog import catalog_server
from openfactors.ingestion.errors import ConnectorError
from openfactors.ingestion.runs import MemoryRunLog, RunRecord
from openfactors.orchestration.daily import FailureAlert
from openfactors.orchestration.status import (
    build_status,
    main,
    note_api,
    note_gate,
)
from openfactors.quality.gates import GateResult

ATTEMPT = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
SUCCESS = datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
FAILED = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)


def test_attempt_success_and_publication_stay_separate(caplog) -> None:
    caplog.set_level(logging.INFO, logger="openfactors.orchestration")
    runs = MemoryRunLog()
    _put(
        runs,
        "attempt-failed",
        status="failed",
        started_at=FAILED,
        finished_at=FAILED + timedelta(seconds=2),
        error_category="schema",
        rows_received=4,
        rows_accepted=0,
    )
    _put(
        runs,
        "attempt-ok",
        status="succeeded",
        started_at=ATTEMPT,
        finished_at=SUCCESS,
        rows_received=4,
        rows_accepted=4,
    )
    _put(
        runs,
        "dry-run",
        status="succeeded",
        stream="backfill-dry-run",
        started_at=FAILED,
        finished_at=FAILED,
    )
    alerts = [
        FailureAlert(
            run_id="attempt-failed",
            manila_date=date(2026, 10, 25),
            step="validate",
            category="schema",
            at=FAILED,
        )
    ]
    quality = []
    note_gate(quality, GateResult(status="fail", violations=("schema",)))
    report = build_status(
        runs,
        alerts,
        quality,
        [],
        publication_at=PUBLISHED,
        now=NOW,
    )
    freshness = report["freshness"]
    stamps = {
        freshness["last_attempt_at"],
        freshness["last_success_at"],
        freshness["last_publication_at"],
    }
    assert freshness["last_attempt_at"] == FAILED.isoformat()
    assert freshness["last_success_at"] == SUCCESS.isoformat()
    assert freshness["last_publication_at"] == PUBLISHED.isoformat()
    assert len(stamps) == 3
    assert freshness["publication_age_seconds"] == 28 * 60 * 60
    assert report["pipeline"]["attempt_count"] == 3
    assert report["pipeline"]["success_count"] == 1
    assert report["pipeline"]["failure_count"] == 1
    failed = next(row for row in report["pipeline"]["runs"] if row["run_id"] == "attempt-failed")
    assert failed["rows_received"] == 4
    assert failed["rows_accepted"] == 0
    assert failed["duration_ms"] == 2000
    assert failed["state"] == "failed"
    assert report["quality"]["failures"] == {"schema": 1}
    assert report["quality"]["latest"] == {"rule": "schema", "passed": False}
    assert report["alerts"] == [
        {
            "run_id": "attempt-failed",
            "step": "validate",
            "category": "schema",
            "manila_date": "2026-10-25",
            "at": FAILED.isoformat(),
        }
    ]
    text = render(report)
    assert "secret-query" not in text
    assert "last_publication=" + PUBLISHED.isoformat() in caplog.text
    assert freshness["last_attempt_at"] not in (freshness["last_publication_at"],)


def test_a_dry_run_and_a_missing_publication_are_not_success(caplog) -> None:
    caplog.set_level(logging.INFO, logger="openfactors.orchestration")
    runs = MemoryRunLog()
    _put(
        runs,
        "dry-run",
        status="succeeded",
        stream="backfill-dry-run",
        started_at=ATTEMPT,
        finished_at=ATTEMPT,
    )
    report = build_status(runs, [], [], [], publication_at=None, now=NOW)
    assert report["freshness"]["last_attempt_at"] == ATTEMPT.isoformat()
    assert report["freshness"]["last_success_at"] is None
    assert report["freshness"]["last_publication_at"] is None
    assert report["freshness"]["publication_age_seconds"] is None
    assert report["pipeline"]["success_count"] == 0
    assert "last_publication=missing" in caplog.text
    assert "last_success=missing" in caplog.text


def test_a_quality_failure_does_not_move_the_publication_time() -> None:
    quality = []
    note_gate(quality, GateResult(status="fail", violations=("schema", "duplicate_location_date")))
    note_gate(quality, GateResult(status="pass", violations=()))
    report = build_status(MemoryRunLog(), [], quality, [], publication_at=PUBLISHED, now=NOW)
    assert report["freshness"]["last_publication_at"] == PUBLISHED.isoformat()
    assert report["freshness"]["last_success_at"] is None
    assert report["quality"]["failures"] == {"duplicate_location_date": 1, "schema": 1}
    assert report["quality"]["latest"]["passed"] is True


def test_api_errors_keep_the_route_and_drop_the_trace(caplog) -> None:
    caplog.set_level(logging.INFO, logger="openfactors.orchestration")
    observations = []

    def observe(path: str, status: int) -> None:
        note_api(observations, path, status)

    def explode() -> list[dict]:
        raise RuntimeError("secret-query")

    server = catalog_server(
        explode,
        load_release=lambda _release_id: None,
        limiter=lambda _method, path: 3 if path == "/api/v1/datasets" else None,
        observe=observe,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _read(server, "/api/v1/no-such-route")
        assert status == 404
        limited, headers, limited_body = _read(server, "/api/v1/datasets")
        assert limited == 429
        assert headers["Retry-After"] == "3"
        failed, _failed_headers, failed_body = _read(server, "/")
        assert failed == 500
        trace = failed_body["error"]["trace_id"]
        queried, _queried_headers, _queried = _read(
            server,
            "/api/v1/factors/daily?location_id=ph-manila&start_date=2018-01-01",
        )
        assert queried == 404
    finally:
        server.shutdown()
        server.server_close()
    report = build_status(
        MemoryRunLog(),
        [],
        [],
        observations,
        publication_at=None,
        now=NOW,
    )
    text = render(report)
    assert trace not in text
    assert "secret-query" not in text
    assert "ph-manila" not in text
    assert "Retry-After" not in text
    requests = {
        (row["route"], row["status_class"]): row["count"] for row in report["api"]["requests"]
    }
    assert requests[("other", "4xx")] == 1
    assert requests[("catalog", "4xx")] == 1
    assert requests[("catalog_page", "5xx")] == 1
    assert requests[("factors", "4xx")] == 1
    assert report["api"]["rate_limited"] == 1
    assert body["error"]["message"] == "That path is not available."
    assert limited_body["error"]["code"] == "RATE_LIMITED"
    page_status, _page_headers, page = _read_closed()
    assert page_status == 200
    assert "last_attempt" not in page
    assert "The operations report is a local command." in page


def test_the_status_command_is_local_and_prints_missing_times(caplog) -> None:
    caplog.set_level(logging.INFO, logger="openfactors.orchestration")
    buffer = io.StringIO()
    assert main([], out=buffer) == 0
    body = json.loads(buffer.getvalue())
    assert body["freshness"]["last_attempt_at"] is None
    assert body["freshness"]["last_success_at"] is None
    assert body["freshness"]["last_publication_at"] is None
    assert "last_publication=missing" in caplog.text
    with pytest.raises(SystemExit, match="does not take arguments"):
        main(["--watch"])
    with pytest.raises(ConnectorError, match="timezone-aware"):
        build_status(MemoryRunLog(), [], [], [], publication_at=None, now=datetime(2026, 10, 25))


def _put(
    runs: MemoryRunLog,
    run_id: str,
    *,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    stream: str = "daily",
    error_category: str | None = None,
    rows_received: int = 0,
    rows_accepted: int = 0,
) -> None:
    runs.runs[run_id] = RunRecord(
        run_id=run_id,
        source_id="daily-external-factors-ph",
        stream=stream,
        location_id="all",
        interval_start=date(2026, 10, 25),
        interval_end=date(2026, 10, 25),
        code_revision="local",
        status=status,
        rows_received=rows_received,
        rows_accepted=rows_accepted,
        error_category=error_category,
        started_at=started_at,
        finished_at=finished_at,
    )


def render(report: dict) -> str:
    from openfactors.orchestration.status import render_status

    return render_status(report)


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


def _read_closed() -> tuple[int, dict[str, str], str]:
    server = catalog_server(lambda: [], load_release=lambda _release_id: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/internal/status", "/api/v1/operations"):
            status, _headers, body = _read(server, path)
            assert status == 404
            assert body["error"]["message"] == "That path is not available."
        return _read(server, "/docs")
    finally:
        server.shutdown()
        server.server_close()
