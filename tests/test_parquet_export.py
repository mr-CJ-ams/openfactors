"""OF-032 Parquet types, release metadata, and row parity with the API."""

from __future__ import annotations

import io
import threading
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import ThreadingHTTPServer

import pyarrow as pa
import pyarrow.parquet as pq
from contracts.check_dataset_contract import CONTRACT_PATH, field_names, load_json
from ops.migrate import apply
from ops.promote import active_release_id
from ops.withdraw import suspend_source
from tests.release_support import (
    ENVIRONMENT,
    cleanup_steps,
    discard_test_releases,
    ensure_isolated_database,
    restore_previous_release,
)
from tests.test_csv_export import _publish, _set_source_status, _source_status
from tests.test_release_manifest import _row

from openfactors.api.catalog import catalog_server
from openfactors.api.csv_export import EXPORT_PATH, csv_bytes, parse_csv
from openfactors.api.factors import _column_names
from openfactors.api.parquet_export import PARQUET_MEDIA_TYPE, parquet_bytes, read_parquet

PIN = "00000000-0000-4000-8000-000000000321"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"
SOURCE_ID = "open-meteo-era5"
QUERY = (
    f"{EXPORT_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-02&format=parquet"
)


def test_parquet_round_trip_keeps_types_and_the_same_values() -> None:
    columns = _column_names()
    assert list(columns) == field_names(load_json(CONTRACT_PATH))
    row = _sample()
    payload = parquet_bytes([row], columns, release_id=PIN, dataset_version="1.0.0")
    rows, metadata = read_parquet(payload)
    schema = pq.read_table(io.BytesIO(payload)).schema
    assert schema.field("date").type == pa.date32()
    assert schema.field("temperature_mean_c").type == pa.decimal128(7, 3)
    assert schema.field("precipitation_mm").type == pa.decimal128(10, 3)
    assert schema.field("source_updated_at").type == pa.timestamp("us", tz="UTC")
    assert metadata == {"dataset_version": "1.0.0", "release_id": PIN}
    assert rows[0]["date"] == date(2018, 1, 1)
    assert rows[0]["temperature_mean_c"] == Decimal("26.100")
    assert rows[0]["precipitation_mm"] is None
    assert rows[0]["holiday_name"] == "=1+1"
    assert rows[0]["is_public_holiday"] is True
    assert rows[0]["is_weekend"] is False
    assert rows[0]["source_updated_at"] == datetime(2026, 10, 24, tzinfo=UTC)
    assert rows[0]["dataset_version"] == "1.0.0"
    csv_row = parse_csv(csv_bytes([row], columns), columns)[0]
    assert csv_row["holiday_name"] == rows[0]["holiday_name"]
    assert csv_row["date"] == "2018-01-01"
    assert csv_row["temperature_mean_c"] == Decimal("26.1")


def test_parquet_response_matches_the_pinned_rows() -> None:
    columns = _column_names()
    row = _sample()
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
        assert headers["Content-Type"] == PARQUET_MEDIA_TYPE
        assert headers["X-Release-Id"] == PIN
        assert headers["X-Dataset-Version"] == "1.0.0"
        assert headers["X-Data-As-Of"] == PUBLISHED_AT
        assert body == parquet_bytes([row], columns, release_id=PIN, dataset_version="1.0.0")
        rows, metadata = read_parquet(body)
        assert metadata["release_id"] == PIN
        assert rows[0]["temperature_mean_c"] == Decimal("26.100")
        assert headers["Content-Disposition"].endswith('.parquet"')
    finally:
        server.shutdown()
        server.server_close()


def test_live_parquet_matches_the_pinned_release() -> None:
    ensure_isolated_database()
    apply(ENVIRONMENT)
    previous = active_release_id(ENVIRONMENT)
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
        rows, metadata = read_parquet(body)
        assert status == 200
        assert headers["X-Release-Id"] == older
        assert metadata["release_id"] == older
        assert metadata["dataset_version"] == "1.0.0"
        assert [row["date"] for row in rows] == [date(2018, 1, 1), date(2018, 1, 2)]
        assert [row["temperature_mean_c"] for row in rows] == [Decimal("26.100"), Decimal("26.200")]
        assert rows[0]["precipitation_mm"] == Decimal("3.400")
        assert rows[0]["holiday_name"] is None
        assert rows[0]["source_updated_at"] is None
        suspend_source(ENVIRONMENT, SOURCE_ID)
        hidden = _read(server, f"{QUERY}&release_id={older}")
        missing = _read(server, f"{QUERY}&release_id={uuid.uuid4()}")
        assert hidden[0] == missing[0] == 404
        assert hidden[2]["error"]["message"] == missing[2]["error"]["message"]
    finally:
        cleanup_steps(
            lambda: _set_source_status(prior),
            lambda: discard_test_releases(ENVIRONMENT, [older, newer]),
            lambda: restore_previous_release(ENVIRONMENT, previous),
            server.shutdown,
            server.server_close,
        )


def _sample() -> dict:
    row = _row()
    row["temperature_mean_c"] = "26.1"
    row["precipitation_mm"] = None
    row["is_public_holiday"] = True
    row["holiday_name"] = "=1+1"
    row["is_weekend"] = False
    row["source_updated_at"] = "2026-10-24T00:00:00Z"
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


def _live_release(release_id: str | None) -> dict | None:
    from openfactors.api.catalog import load_accessible_release

    return load_accessible_release(ENVIRONMENT, release_id)


def _live_rows(filters: dict) -> list[dict]:
    from openfactors.api.factors import load_export_rows

    return load_export_rows(ENVIRONMENT, **filters)


def _read(server: ThreadingHTTPServer, path: str) -> tuple[int, dict[str, str], bytes | dict]:
    import json
    import urllib.error

    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read()
            if "parquet" not in response.headers.get("Content-Type", ""):
                payload = json.loads(payload.decode("utf-8"))
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
        return error.code, dict(error.headers), payload
