"""OF-033 export ceilings, ETag, attribution manifest, and aborted files."""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from tests.test_release_manifest import _row

from openfactors.api.catalog import REGISTRY_PATH, catalog_server
from openfactors.api.csv_export import EXPORT_PATH, csv_bytes, handle_export
from openfactors.api.export_limits import (
    MANIFEST_MEDIA_TYPE,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_ROWS,
    commit_export,
    etag,
    limit_message,
)
from openfactors.api.factors import _column_names

PIN = "00000000-0000-4000-8000-000000000331"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"
QUERY = f"{EXPORT_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-02&format=csv"
WEATHER = "Weather data by Open-Meteo.com"
HOLIDAY = "National holidays as proclaimed in the Official Gazette of the Philippines"


def test_ceilings_match_the_recorded_provisional_caps() -> None:
    signature = inspect.signature(handle_export)
    assert signature.parameters["max_rows"].default == MAX_EXPORT_ROWS == 100_000
    assert signature.parameters["max_bytes"].default == MAX_EXPORT_BYTES == 50_000_000
    assert limit_message("rows", MAX_EXPORT_ROWS) == (
        "The export exceeds 100000 rows. Split the date interval and request each part separately."
    )
    assert limit_message("bytes", MAX_EXPORT_BYTES) == (
        "The export exceeds 50000000 bytes. "
        "Split the date interval and request each part separately."
    )


def test_oversize_export_returns_422_and_writes_nothing(tmp_path: Path) -> None:
    rows = [_sample("2018-01-01"), _sample("2018-01-02")]
    status, headers, payload = handle_export(
        "GET",
        QUERY,
        load_release=_release,
        load_rows=lambda **_filters: rows,
        artifact_dir=lambda release_id: tmp_path / release_id,
        max_rows=1,
    )
    assert status == 422
    assert headers == {}
    assert isinstance(payload, dict)
    assert payload["error"]["code"] == "EXPORT_LIMIT"
    assert payload["error"]["details"] == {"limit": 1, "unit": "rows"}
    assert "Split the date interval" in payload["error"]["message"]
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
    file_body = csv_bytes(rows[:1], _column_names())
    limited, limited_headers, limited_payload = handle_export(
        "GET",
        QUERY,
        load_release=_release,
        load_rows=lambda **_filters: rows[:1],
        artifact_dir=lambda release_id: tmp_path / release_id,
        max_bytes=len(file_body) - 1,
        request_headers={"Accept": MANIFEST_MEDIA_TYPE},
    )
    assert limited == 422
    assert limited_headers == {}
    assert isinstance(limited_payload, dict)
    assert limited_payload["error"]["details"]["unit"] == "bytes"
    assert "sha256" not in json.dumps(limited_payload)
    assert list(tmp_path.iterdir()) == []


def test_exact_ceiling_is_sent_with_etag_and_manifest(tmp_path: Path) -> None:
    rows = [_sample("2018-01-01")]
    file_body = csv_bytes(rows, _column_names())
    status, headers, payload = handle_export(
        "GET",
        QUERY,
        load_release=_release,
        load_rows=lambda **_filters: rows,
        artifact_dir=lambda release_id: tmp_path / release_id,
        max_rows=1,
        max_bytes=len(file_body),
    )
    assert status == 200
    assert payload == file_body
    assert headers["ETag"] == etag(file_body)
    assert headers["ETag"] == f'"{hashlib.sha256(file_body).hexdigest()}"'
    assert f"/api/v1/datasets/daily-external-factors-ph?release_id={PIN}" in headers["Link"]
    assert "https://open-meteo.com/" in headers["Link"]
    assert "https://www.officialgazette.gov.ph/" in headers["Link"]
    stored = tmp_path / PIN
    data_name = next(path.name for path in stored.iterdir() if path.suffix == ".csv")
    sidecar = json.loads((stored / f"{data_name}.manifest.json").read_text(encoding="utf-8"))
    assert sidecar["sha256"] == headers["ETag"].strip('"')
    assert sidecar["row_count"] == 1
    assert sidecar["byte_count"] == len(file_body)
    assert {item["attribution"] for item in sidecar["sources"]} == {WEATHER, HOLIDAY}
    manifest_status, manifest_headers, manifest_body = handle_export(
        "GET",
        QUERY,
        load_release=_release,
        load_rows=lambda **_filters: rows,
        request_headers={"Accept": MANIFEST_MEDIA_TYPE},
        max_bytes=len(file_body),
    )
    assert manifest_status == 200
    assert manifest_headers["Content-Type"] == MANIFEST_MEDIA_TYPE
    assert manifest_headers["Content-Disposition"].endswith('.manifest.json"')
    document = json.loads(manifest_body)
    assert document["sha256"] == hashlib.sha256(file_body).hexdigest()
    assert manifest_headers["ETag"] == etag(manifest_body)


def test_missing_attribution_and_a_failed_read_publish_no_file(tmp_path: Path) -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    registry["sources"][0]["attribution"] = ""
    status, headers, payload = handle_export(
        "GET",
        QUERY,
        load_release=_release,
        load_rows=lambda **_filters: [_sample("2018-01-01")],
        registry=registry,
        artifact_dir=lambda release_id: tmp_path / release_id,
    )
    assert status == 500
    assert headers == {}
    assert payload["error"]["code"] == "INTERNAL"
    assert payload["error"]["message"] == "The export failed."
    assert "attribution" not in json.dumps(payload)
    assert list(tmp_path.iterdir()) == []

    def fail(**_filters: object) -> list[dict]:
        raise RuntimeError("secret-query")

    failed, _failed_headers, failed_payload = handle_export(
        "GET",
        QUERY,
        load_release=_release,
        load_rows=fail,
        artifact_dir=lambda release_id: tmp_path / "other",
    )
    assert failed == 500
    assert "secret-query" not in json.dumps(failed_payload)
    assert list(tmp_path.iterdir()) == []


def test_aborted_publish_leaves_no_file(tmp_path: Path) -> None:
    def stop(stage: str) -> None:
        if stage == "manifest":
            raise OSError("aborted")

    with pytest.raises(OSError, match="aborted"):
        commit_export(tmp_path, "daily.csv", b"date\n", b"{}\n", checkpoint=stop)
    assert list(tmp_path.iterdir()) == []


def test_export_route_keeps_the_unset_request_limiter() -> None:
    limited = catalog_server(
        lambda: [],
        load_release=_release,
        load_export=lambda **_filters: [_sample("2018-01-01")],
        limiter=lambda _method, _path: 7,
    )
    open_server = catalog_server(
        lambda: [],
        load_release=_release,
        load_export=lambda **_filters: [_sample("2018-01-01")],
    )
    limited_thread = threading.Thread(target=limited.serve_forever, daemon=True)
    open_thread = threading.Thread(target=open_server.serve_forever, daemon=True)
    limited_thread.start()
    open_thread.start()
    try:
        status, headers, payload = _read(limited, QUERY)
        assert status == 429
        assert headers["Retry-After"] == "7"
        assert payload["error"]["code"] == "RATE_LIMITED"
        allowed, allowed_headers, body = _read(open_server, QUERY)
        assert allowed == 200
        assert allowed_headers["ETag"] == etag(body)
        assert "Retry-After" not in allowed_headers
    finally:
        limited.shutdown()
        limited.server_close()
        open_server.shutdown()
        open_server.server_close()


def _sample(day: str) -> dict:
    row = _row()
    row["date"] = day
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


def _read(server: ThreadingHTTPServer, path: str) -> tuple[int, dict[str, str], bytes | dict]:
    host, port = server.server_address
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
