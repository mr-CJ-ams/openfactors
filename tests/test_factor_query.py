"""OF-028 factor filters, release pin, and deterministic pagination."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import date
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml
from contracts.calendar import date_parts
from contracts.check_dataset_contract import CONTRACT_PATH, field_names, load_json
from ops.migrate import apply, psql
from ops.promote import active_release_id, promote_release
from ops.releases import persist_manifest
from ops.withdraw import suspend_source
from tests.release_support import (
    ENVIRONMENT,
    cleanup_steps,
    discard_test_releases,
    ensure_isolated_database,
    restore_previous_release,
)
from tests.test_release_manifest import _build, _row

from openfactors.api.catalog import CatalogUnavailable, catalog_server
from openfactors.api.factors import FACTORS_PATH

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"
SOURCE_ID = "open-meteo-era5"
ALLOWED_STATUS = {"unreviewed", "approved", "rejected", "suspended"}
OLDER = "00000000-0000-4000-8000-000000000281"
NEWER = "00000000-0000-4000-8000-000000000282"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"
QUERY = f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-02"


def test_openapi_factor_query_requires_location_and_inclusive_dates() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    operation = document["paths"][FACTORS_PATH]["get"]
    required = {item["name"]: item["required"] for item in operation["parameters"]}
    schema = document["components"]["schemas"]["FactorPage"]
    factor = document["components"]["schemas"]["DailyFactor"]["required"]
    assert operation["operationId"] == "listDailyFactors"
    assert required["location_id"] is True
    assert required["start_date"] is True
    assert required["end_date"] is True
    assert required["release_id"] is False
    assert required["page"] is False
    assert required["page_size"] is False
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/FactorPage"
    )
    assert schema["allOf"][1]["required"] == ["pagination", "data"]
    assert factor == field_names(load_json(CONTRACT_PATH))


def test_pages_stay_inside_the_pinned_release() -> None:
    calls: list[str | None] = []
    server = catalog_server(lambda: [], load_release=_remember(calls), load_factors=_pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first_status, first_headers, first = _request(server, f"{QUERY}&page_size=1")
        assert calls == [None]
        second_status, _second_headers, second = _request(
            server, f"{QUERY}&release_id={OLDER}&page=2&page_size=1"
        )
        empty_status, _empty_headers, empty = _request(
            server, f"{QUERY}&release_id={OLDER}&page=3&page_size=1"
        )
        day_status, _day_headers, day = _request(
            server,
            f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-02&end_date=2018-01-02"
            f"&release_id={OLDER}",
        )
        assert first_status == second_status == empty_status == day_status == 200
        assert first_headers["X-Release-Id"] == first["release_id"] == NEWER
        assert [row["date"] for row in first["data"]] == ["2018-01-01"]
        assert [row["date"] for row in second["data"]] == ["2018-01-02"]
        assert first["data"][0]["temperature_mean_c"] == 18.4
        assert second["data"][0]["temperature_mean_c"] == 26.2
        assert empty["data"] == []
        assert empty["pagination"] == {"page": 3, "page_size": 1, "total": 2}
        assert [row["date"] for row in day["data"]] == ["2018-01-02"]
        assert list(first["data"][0]) == field_names(load_json(CONTRACT_PATH))
        missing_status, _missing_headers, missing = _request(
            server, f"{FACTORS_PATH}?start_date=2018-01-01&end_date=2018-01-01"
        )
        inverted_status, _inverted_headers, inverted = _request(
            server,
            f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-02&end_date=2018-01-01",
        )
        size_status, _size_headers, size = _request(server, f"{QUERY}&page_size=1001")
        page_status, _page_headers, page = _request(server, f"{QUERY}&page=0")
        unknown_status, _unknown_headers, unknown = _request(
            server,
            f"{FACTORS_PATH}?location_id=ph-nowhere&start_date=2018-01-01&end_date=2018-01-02",
        )
        hidden_status, _hidden_headers, hidden = _request(
            server, f"{QUERY}&release_id={uuid.uuid4()}"
        )
        other_status, _other_headers, other = _request(server, f"{QUERY}&release_id={uuid.uuid4()}")
        assert missing_status == inverted_status == size_status == page_status == 422
        assert missing["error"]["code"] == "MISSING_QUERY_PARAMETER"
        assert inverted["error"]["code"] == "INVALID_DATE_RANGE"
        assert inverted["error"]["message"] == "start_date must be on or before end_date"
        assert size["error"]["code"] == "INVALID_PAGE_SIZE"
        assert page["error"]["code"] == "INVALID_PAGE"
        assert unknown_status == hidden_status == other_status == 404
        assert unknown["error"]["message"] == "That location is not available."
        assert hidden["error"]["message"] == other["error"]["message"]
        assert "withdrawn" not in hidden["error"]["message"].lower()
    finally:
        server.shutdown()
        server.server_close()


def test_concurrent_pins_do_not_mix_rows() -> None:
    started = threading.Barrier(2)
    server = catalog_server(
        lambda: [],
        load_release=_release_for,
        load_factors=lambda **filters: _slow_page(started, filters),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results: dict[str, dict] = {}

    def read(release_id: str) -> None:
        _status, _headers, body = _request(server, f"{QUERY}&release_id={release_id}")
        results[release_id] = body

    workers = [
        threading.Thread(target=read, args=(OLDER,)),
        threading.Thread(target=read, args=(NEWER,)),
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert [row["temperature_mean_c"] for row in results[OLDER]["data"]] == [26.1, 26.2]
        assert [row["temperature_mean_c"] for row in results[NEWER]["data"]] == [18.4, 18.5]
        assert results[OLDER]["release_id"] == OLDER
        assert results[NEWER]["release_id"] == NEWER
    finally:
        server.shutdown()
        server.server_close()


def test_factor_failures_hide_the_database_error() -> None:
    server = catalog_server(
        lambda: [],
        load_release=lambda _release_id: (_ for _ in ()).throw(CatalogUnavailable()),
        load_factors=_pages,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _request(server, QUERY)
        encoded = json.dumps(body)
        assert status == 503
        assert body["error"]["code"] == "FACTORS_UNAVAILABLE"
        assert body["error"]["trace_id"]
        assert "password" not in encoded.lower()
        assert "select" not in encoded.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_live_pin_stays_stable_when_a_newer_release_is_active() -> None:
    ensure_isolated_database()
    apply(ENVIRONMENT)
    previous = active_release_id(ENVIRONMENT)
    prior = _source_status(ENVIRONMENT)
    older = str(uuid.uuid4())
    newer = str(uuid.uuid4())
    server = catalog_server(
        lambda: [],
        load_release=lambda release_id: _live_release(release_id),
        load_factors=lambda **filters: _live_page(filters),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if prior != "approved":
            _set_source_status("approved", ENVIRONMENT)
        _publish(older, ("26.1", "26.2"))
        _publish(newer, ("18.4", "18.5"))
        current_status, current_headers, current = _request(server, f"{QUERY}&page_size=1")
        pinned_status, _pinned_headers, pinned = _request(
            server, f"{QUERY}&release_id={older}&page=2&page_size=1"
        )
        inclusive_status, _inclusive_headers, inclusive = _request(
            server,
            f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-01"
            f"&end_date=2018-01-01&release_id={older}",
        )
        beyond_status, _beyond_headers, beyond = _request(
            server,
            f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-03"
            f"&end_date=2018-01-03&release_id={older}",
        )
        assert current_status == pinned_status == inclusive_status == beyond_status == 200
        assert current_headers["X-Release-Id"] == current["release_id"] == newer
        assert _temperature(current["data"][0]) == Decimal("18.4")
        assert pinned["release_id"] == older
        assert pinned["data"][0]["date"] == "2018-01-02"
        assert _temperature(pinned["data"][0]) == Decimal("26.2")
        assert inclusive["pagination"]["total"] == 1
        assert inclusive["data"][0]["date"] == "2018-01-01"
        assert beyond["data"] == []
        assert beyond["pagination"]["total"] == 0
        suspend_source(ENVIRONMENT, SOURCE_ID)
        hidden_status, _hidden_headers, hidden = _request(server, f"{QUERY}&release_id={older}")
        missing_status, _missing_headers, missing = _request(
            server, f"{QUERY}&release_id={uuid.uuid4()}"
        )
        assert hidden_status == missing_status == 404
        assert hidden["error"]["message"] == missing["error"]["message"]
        assert "withdrawn" not in hidden["error"]["message"].lower()
    finally:
        cleanup_steps(
            lambda: _set_source_status(prior, ENVIRONMENT),
            lambda: discard_test_releases(ENVIRONMENT, [older, newer]),
            lambda: restore_previous_release(ENVIRONMENT, previous),
            server.shutdown,
            server.server_close,
        )


def _remember(calls: list[str | None]):
    def load(release_id: str | None) -> dict | None:
        calls.append(release_id)
        if release_id in (None, NEWER):
            return _metadata(NEWER)
        if release_id == OLDER:
            return _metadata(OLDER)
        return None

    return load


def _release_for(release_id: str | None) -> dict | None:
    if release_id == OLDER:
        return _metadata(OLDER)
    if release_id == NEWER:
        return _metadata(NEWER)
    return None


def _metadata(release_id: str) -> dict:
    return {
        "dataset_id": "daily_external_factors_ph",
        "release_id": release_id,
        "dataset_version": "1.0.0",
        "published_at": PUBLISHED_AT,
    }


def _pages(**filters: object) -> dict:
    release_id = str(filters["release_id"])
    temperatures = ("26.1", "26.2") if release_id == OLDER else ("18.4", "18.5")
    rows = [
        _public_row("2018-01-01", temperatures[0]),
        _public_row("2018-01-02", temperatures[1]),
    ]
    start = str(filters["start_date"])
    end = str(filters["end_date"])
    matched = [row for row in rows if start <= row["date"] <= end]
    page = int(filters["page"])
    page_size = int(filters["page_size"])
    offset = (page - 1) * page_size
    return {"total": len(matched), "rows": matched[offset : offset + page_size]}


def _slow_page(started: threading.Barrier, filters: dict) -> dict:
    started.wait(timeout=5)
    return _pages(**filters)


def _public_row(day: str, temperature: str) -> dict:
    row = _row()
    row["date"] = day
    row["temperature_mean_c"] = float(temperature)
    row["precipitation_mm"] = 3.4
    row["is_weekend"] = date_parts(date.fromisoformat(day))["is_weekend"]
    return row


def _publish(release_id: str, temperatures: tuple[str, str]) -> None:
    rows = [
        _stored_row(day, temperature)
        for day, temperature in zip(("2018-01-01", "2018-01-02"), temperatures, strict=True)
    ]
    manifest = _build(
        load_json(CONTRACT_PATH),
        rows,
        release_id=release_id,
        dates={"2018-01-01", "2018-01-02"},
        supported={("ph-manila", "2018-01-01"), ("ph-manila", "2018-01-02")},
        verified_dates={
            "2018-01-01": {"is_public_holiday": False, "holiday_name": None},
            "2018-01-02": {"is_public_holiday": False, "holiday_name": None},
        },
        coverage_start="2018-01-01",
        coverage_end="2018-01-02",
    )
    assert manifest.status == "approved", manifest.coverage.violations
    persist_manifest(ENVIRONMENT, manifest)
    promote_release(ENVIRONMENT, manifest, rows)


def _stored_row(day: str, temperature: str) -> dict:
    row = _row()
    row["date"] = day
    row["temperature_mean_c"] = temperature
    row["is_weekend"] = date_parts(date.fromisoformat(day))["is_weekend"]
    return row


def _live_release(release_id: str | None) -> dict | None:
    from openfactors.api.catalog import load_accessible_release

    return load_accessible_release(ENVIRONMENT, release_id)


def _live_page(filters: dict) -> dict:
    from openfactors.api.factors import load_factor_page

    return load_factor_page(ENVIRONMENT, **filters)


def _temperature(row: dict) -> Decimal:
    return Decimal(str(row["temperature_mean_c"]))


def _request(
    server: ThreadingHTTPServer, path: str, method: str = "GET"
) -> tuple[int, dict[str, str], dict]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
        return error.code, dict(error.headers), payload


def _source_status(environment: str = "dev") -> str:
    return _admin(
        "SELECT redistribution_status FROM control.source_registry "
        f"WHERE source_id = '{SOURCE_ID}';",
        environment,
    )[0]


def _set_source_status(status: str, environment: str = "dev") -> None:
    if status not in ALLOWED_STATUS:
        raise AssertionError(status)
    completed = psql(
        environment,
        "of_admin",
        "UPDATE control.source_registry "
        f"SET redistribution_status = '{status}', updated_at = now() "
        f"WHERE source_id = '{SOURCE_ID}';",
    )
    assert completed.returncode == 0, completed.stderr


def _admin(sql: str, environment: str = "dev") -> list[str]:
    completed = psql(environment, "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]
