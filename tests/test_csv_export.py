"""OF-031 CSV headers, nulls, timestamps, and formula prefixes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import date
from decimal import Decimal
from http.server import ThreadingHTTPServer

import pytest
from contracts.calendar import date_parts
from contracts.check_dataset_contract import CONTRACT_PATH, field_names, load_json
from ops.local_stack import docker_available
from ops.migrate import apply, psql
from ops.promote import promote_release
from ops.releases import persist_manifest
from ops.withdraw import suspend_source
from tests.test_release_manifest import _build, _row

from openfactors.api.catalog import catalog_server
from openfactors.api.csv_export import (
    EXPORT_PATH,
    csv_bytes,
    guard_formula,
    parse_csv,
    restore_formula,
)
from openfactors.api.factors import _column_names

SOURCE_ID = "open-meteo-era5"
ALLOWED_STATUS = {"unreviewed", "approved", "rejected", "suspended"}
PIN = "00000000-0000-4000-8000-000000000311"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"
QUERY = f"{EXPORT_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-02&format=csv"


def test_csv_round_trip_guards_formula_text_and_keeps_nulls() -> None:
    columns = _column_names()
    assert list(columns) == field_names(load_json(CONTRACT_PATH))
    row = _sample("=1+1", "2026-10-24T00:00:00Z")
    row["holiday_name"] = "=1+1"
    prefixed = [
        "=1+1",
        "+1",
        "-2",
        "@SUM(A1)",
        "\t=1",
        "\r=1",
        "Día",
        "Alpha, Beta",
    ]
    for name in prefixed:
        assert restore_formula(guard_formula(name)) == name
    assert guard_formula("'=kept") == "'=kept"
    assert restore_formula("'=kept") == "=kept"
    guarded = csv_bytes([row], columns)
    assert guarded.startswith(b"date,location_id,")
    assert b"\xef\xbb\xbf" not in guarded
    parsed = parse_csv(guarded, columns)
    assert parsed[0]["holiday_name"] == "=1+1"
    assert parsed[0]["temperature_mean_c"] == Decimal("26.1")
    assert parsed[0]["precipitation_mm"] == Decimal("0")
    assert parsed[0]["source_updated_at"] == "2026-10-24T00:00:00Z"
    assert parsed[0]["is_public_holiday"] is True
    empty = _sample(None, None)
    empty["temperature_mean_c"] = None
    empty["is_public_holiday"] = False
    empty["holiday_name"] = None
    restored = parse_csv(csv_bytes([empty], columns), columns)[0]
    assert restored["temperature_mean_c"] is None
    assert restored["holiday_name"] is None
    assert restored["source_updated_at"] is None
    assert restored["is_weekend"] is False


def test_export_response_matches_the_pinned_rows() -> None:
    columns = _column_names()
    row = _sample("=1+1", "2026-10-24T00:00:00Z")
    server = catalog_server(
        lambda: [],
        load_release=lambda release_id: _release(release_id),
        load_export=lambda **_filters: [row],
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _read(server, QUERY)
        assert status == 200
        assert headers["Content-Type"].startswith("text/csv")
        assert "charset=utf-8" in headers["Content-Type"]
        assert headers["X-Release-Id"] == PIN
        assert headers["X-Dataset-Version"] == "1.0.0"
        assert headers["X-Data-As-Of"] == PUBLISHED_AT
        assert headers["Content-Disposition"].endswith('.csv"')
        assert body == csv_bytes([row], columns)
        assert parse_csv(body, columns)[0]["holiday_name"] == "=1+1"
        hidden_status, _hidden_headers, hidden = _read(server, f"{QUERY}&release_id={uuid.uuid4()}")
        other_status, _other_headers, other = _read(server, f"{QUERY}&release_id={uuid.uuid4()}")
        assert hidden_status == other_status == 404
        assert hidden["error"]["message"] == other["error"]["message"]
        assert "withdrawn" not in hidden["error"]["message"].lower()
    finally:
        server.shutdown()
        server.server_close()


def test_live_csv_uses_the_pinned_release() -> None:
    if not docker_available():
        pytest.skip("Docker is required to export a published release.")
    apply("dev")
    prior = _source_status()
    older = str(uuid.uuid4())
    newer = str(uuid.uuid4())
    server = catalog_server(
        lambda: [],
        load_release=lambda release_id: _live_release(release_id),
        load_export=lambda **filters: _live_rows(filters),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if prior != "approved":
            _set_source_status("approved")
        _publish(older, ("26.1", "26.2"))
        _publish(newer, ("18.4", "18.5"))
        status, headers, body = _read(server, f"{QUERY}&release_id={older}")
        rows = parse_csv(body, _column_names())
        assert status == 200
        assert headers["X-Release-Id"] == older
        assert [row["date"] for row in rows] == ["2018-01-01", "2018-01-02"]
        assert [row["temperature_mean_c"] for row in rows] == [Decimal("26.1"), Decimal("26.2")]
        assert rows[0]["holiday_name"] is None
        assert rows[0]["source_updated_at"] is None
        suspend_source("dev", SOURCE_ID)
        hidden_status, _hidden_headers, hidden = _read(server, f"{QUERY}&release_id={older}")
        missing_status, _missing_headers, missing = _read(
            server, f"{QUERY}&release_id={uuid.uuid4()}"
        )
        assert hidden_status == missing_status == 404
        assert hidden["error"]["message"] == missing["error"]["message"]
    finally:
        _set_source_status(prior)
        _cleanup([older, newer])
        server.shutdown()
        server.server_close()


def _sample(holiday_name: str | None, updated_at: str | None) -> dict:
    row = _row()
    row["holiday_name"] = holiday_name
    row["is_public_holiday"] = holiday_name is not None
    row["precipitation_mm"] = 0
    row["source_updated_at"] = updated_at
    row["is_weekend"] = False
    return row


def _release(release_id: str | None) -> dict | None:
    if release_id not in (None, PIN):
        return None
    return {
        "dataset_id": "daily_external_factors_ph",
        "release_id": PIN,
        "dataset_version": "1.0.0",
        "published_at": PUBLISHED_AT,
    }


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
    persist_manifest("dev", manifest)
    promote_release("dev", manifest, rows)


def _stored_row(day: str, temperature: str) -> dict:
    row = _row()
    row["date"] = day
    row["temperature_mean_c"] = temperature
    row["is_weekend"] = date_parts(date.fromisoformat(day))["is_weekend"]
    return row


def _live_release(release_id: str | None) -> dict | None:
    from openfactors.api.catalog import load_accessible_release

    return load_accessible_release("dev", release_id)


def _live_rows(filters: dict) -> list[dict]:
    from openfactors.api.factors import load_export_rows

    return load_export_rows("dev", **filters)


def _read(server: ThreadingHTTPServer, path: str) -> tuple[int, dict[str, str], bytes | dict]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read()
            if "text/csv" not in response.headers.get("Content-Type", ""):
                payload = json.loads(payload.decode("utf-8"))
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
        return error.code, dict(error.headers), payload


def _source_status() -> str:
    return _admin(
        "SELECT redistribution_status FROM control.source_registry "
        f"WHERE source_id = '{SOURCE_ID}';"
    )[0]


def _set_source_status(status: str) -> None:
    if status not in ALLOWED_STATUS:
        raise AssertionError(status)
    completed = psql(
        "dev",
        "of_admin",
        "UPDATE control.source_registry "
        f"SET redistribution_status = '{status}', updated_at = now() "
        f"WHERE source_id = '{SOURCE_ID}';",
    )
    assert completed.returncode == 0, completed.stderr


def _admin(sql: str) -> list[str]:
    completed = psql("dev", "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]


def _cleanup(release_ids: list[str]) -> None:
    quoted = ", ".join(f"'{release_id}'::uuid" for release_id in release_ids)
    completed = psql(
        "dev",
        "of_admin",
        f"""
        BEGIN;
        ALTER TABLE publish.daily_external_factors_ph DISABLE TRIGGER published_rows_immutable;
        DELETE FROM publish.daily_external_factors_ph WHERE release_id IN ({quoted});
        DELETE FROM publish.active_dataset_release WHERE release_id IN ({quoted});
        ALTER TABLE publish.daily_external_factors_ph ENABLE TRIGGER published_rows_immutable;
        COMMIT;
        """,
    )
    assert completed.returncode == 0, completed.stderr
