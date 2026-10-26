"""OF-027 dictionary schema, release pin, and 404 for an inaccessible release."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml
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

from openfactors.api.catalog import DATASET_PATH, catalog_server

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"
DICTIONARY = ROOT / "contracts" / "fixtures" / "data_dictionary.json"
SOURCE_ID = "open-meteo-era5"
ALLOWED_STATUS = {"unreviewed", "approved", "rejected", "suspended"}
ACTIVE = "00000000-0000-4000-8000-000000000271"
OLDER = "00000000-0000-4000-8000-000000000270"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"


def test_openapi_dictionary_requires_the_twelve_field_shape() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    operation = document["paths"][DATASET_PATH]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    parameter = operation["parameters"][0]
    assert operation["operationId"] == "getDataset"
    assert parameter["name"] == "release_id"
    assert parameter["required"] is False
    assert schema["required"] == [
        "release",
        "grain",
        "timezone",
        "columns",
        "sources",
        "license",
        "coverage",
        "limitations",
    ]


def test_dictionary_pins_a_release_and_hides_an_unknown_one() -> None:
    dictionary = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    server = catalog_server(lambda: [], load_release=_releases)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _request(server, DATASET_PATH)
        assert status == 200
        _assert_dictionary(body, dictionary, ACTIVE)
        pinned_status, _pinned_headers, pinned = _request(
            server, f"{DATASET_PATH}?release_id={OLDER}"
        )
        assert pinned_status == 200
        assert pinned["release"]["release_id"] == OLDER
        assert pinned["release"]["data_as_of"] == PUBLISHED_AT
        assert pinned["release"]["generated_at"] != pinned["release"]["data_as_of"]
        unknown_id = str(uuid.uuid4())
        other_id = str(uuid.uuid4())
        unknown_status, _unknown_headers, unknown = _request(
            server, f"{DATASET_PATH}?release_id={unknown_id}"
        )
        other_status, _other_headers, other = _request(
            server, f"{DATASET_PATH}?release_id={other_id}"
        )
        assert unknown_status == other_status == 404
        assert unknown["error"]["code"] == other["error"]["code"] == "NOT_FOUND"
        assert unknown["error"]["message"] == other["error"]["message"]
        assert "withdrawn" not in unknown["error"]["message"].lower()
        invalid_status, _invalid_headers, invalid = _request(
            server, f"{DATASET_PATH}?release_id=not-a-uuid"
        )
        assert invalid_status == 422
        assert invalid["error"]["code"] == "INVALID_RELEASE_ID"
        extra_status, _extra_headers, extra = _request(server, f"{DATASET_PATH}?page=1")
        assert extra_status == 422
        assert extra["error"]["code"] == "UNKNOWN_QUERY_PARAMETER"
    finally:
        server.shutdown()
        server.server_close()


def test_live_pin_survives_a_newer_release_and_fails_after_withdrawal() -> None:
    ensure_isolated_database()
    apply(ENVIRONMENT)
    previous = active_release_id(ENVIRONMENT)
    prior = _source_status()
    older = str(uuid.uuid4())
    newer = str(uuid.uuid4())
    server = catalog_server(
        lambda: [],
        load_release=lambda release_id: _live_release(release_id),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if prior != "approved":
            _set_source_status("approved")
        _publish(older, "26.1")
        _publish(newer, "18.4")
        current_status, _current_headers, current = _request(server, DATASET_PATH)
        pinned_status, _pinned_headers, pinned = _request(
            server, f"{DATASET_PATH}?release_id={older}"
        )
        assert current_status == pinned_status == 200
        assert current["release"]["release_id"] == newer
        assert pinned["release"]["release_id"] == older
        assert len(pinned["columns"]) == 12
        suspend_source(ENVIRONMENT, SOURCE_ID)
        hidden_status, _hidden_headers, hidden = _request(
            server, f"{DATASET_PATH}?release_id={older}"
        )
        missing_status, _missing_headers, missing = _request(
            server, f"{DATASET_PATH}?release_id={uuid.uuid4()}"
        )
        assert hidden_status == missing_status == 404
        assert hidden["error"]["message"] == missing["error"]["message"]
        assert "withdrawn" not in hidden["error"]["message"].lower()
    finally:
        cleanup_steps(
            lambda: _set_source_status(prior),
            lambda: discard_test_releases(ENVIRONMENT, [older, newer]),
            lambda: restore_previous_release(ENVIRONMENT, previous),
            server.shutdown,
            server.server_close,
        )


def _assert_dictionary(body: dict, dictionary: dict, release_id: str) -> None:
    assert [column["name"] for column in body["columns"]] == field_names(load_json(CONTRACT_PATH))
    assert "weather_data_status" not in {column["name"] for column in body["columns"]}
    assert set(body["columns"][0]) == {
        "name",
        "logical_type",
        "physical_type",
        "nullable",
        "unit",
        "description",
        "source",
        "transformation",
        "examples",
    }
    assert [source["source_id"] for source in body["sources"]] == [
        "open-meteo-era5",
        "ph-official-gazette",
    ]
    assert dictionary["known_gaps"] in body["limitations"]
    assert dictionary["provider_spatial_resolution"] in body["limitations"]
    assert "local holidays" in body["limitations"]
    assert body["timezone"] == "Asia/Manila"
    assert body["grain"] == dictionary["grain"]
    assert body["license"]["summary"] == dictionary["license"]
    assert body["release"]["release_id"] == release_id
    assert body["release"]["dataset_version"] == "1.0.0"
    assert body["coverage"]["start"] == "2018-01-01"
    assert body["coverage"]["end"] == "2018-01-01"


def _releases(release_id: str | None) -> dict | None:
    if release_id in (None, ACTIVE):
        return _release(ACTIVE)
    if release_id == OLDER:
        return _release(OLDER)
    return None


def _release(release_id: str) -> dict:
    return {
        "dataset_id": "daily_external_factors_ph",
        "release_id": release_id,
        "dataset_version": "1.0.0",
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
        "published_at": PUBLISHED_AT,
    }


def _publish(release_id: str, temperature: str) -> None:
    row = _row()
    row["temperature_mean_c"] = temperature
    manifest = _build(load_json(CONTRACT_PATH), [row], release_id=release_id)
    persist_manifest(ENVIRONMENT, manifest)
    promote_release(ENVIRONMENT, manifest, [row])


def _live_release(release_id: str | None) -> dict | None:
    from openfactors.api.catalog import load_accessible_release

    return load_accessible_release(ENVIRONMENT, release_id)


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


def _source_status() -> str:
    return _admin(
        "SELECT redistribution_status FROM control.source_registry "
        f"WHERE source_id = '{SOURCE_ID}';"
    )[0]


def _set_source_status(status: str) -> None:
    if status not in ALLOWED_STATUS:
        raise AssertionError(status)
    completed = psql(
        ENVIRONMENT,
        "of_admin",
        "UPDATE control.source_registry "
        f"SET redistribution_status = '{status}', updated_at = now() "
        f"WHERE source_id = '{SOURCE_ID}';",
    )
    assert completed.returncode == 0, completed.stderr


def _admin(sql: str) -> list[str]:
    completed = psql(ENVIRONMENT, "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]
