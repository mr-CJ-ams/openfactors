"""OF-026 catalog endpoint: published metadata, coverage, license, and freshness."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.local_stack import dev_database_ready
from ops.migrate import apply, psql
from ops.promote import active_release_id, promote_release, restore_active_release
from ops.releases import persist_manifest
from ops.withdraw import suspend_source
from tests.test_release_manifest import _build, _row

from openfactors.api.catalog import CatalogUnavailable, catalog_server, handle_catalog

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"
DICTIONARY = ROOT / "contracts" / "fixtures" / "data_dictionary.json"
SOURCE_ID = "open-meteo-era5"
ALLOWED_STATUS = {"unreviewed", "approved", "rejected", "suspended"}
RELEASE_ID = "00000000-0000-4000-8000-000000000260"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"


def test_openapi_catalog_lists_datasets_and_generated_at() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    operation = document["paths"]["/api/v1/datasets"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert operation["operationId"] == "listDatasets"
    assert operation["parameters"] == []
    assert schema["required"] == ["datasets", "generated_at"]


def test_catalog_http_hides_internal_fields_and_rejects_unknown_queries() -> None:
    dictionary = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    server = catalog_server(lambda: [_published_row()])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _request(server, "/api/v1/datasets")
        assert status == 200
        assert set(body) == {"datasets", "generated_at"}
        assert len(body["datasets"]) == 1
        dataset = body["datasets"][0]
        assert dataset["dataset_id"] == dictionary["dataset_id"]
        assert dataset["active_release_id"] == RELEASE_ID
        assert dataset["dataset_version"] == "1.0.0"
        assert dataset["grain"] == dictionary["grain"]
        assert dataset["coverage"] == {
            "start": "2018-01-01",
            "end": "2018-01-01",
            "geographic": dictionary["geographic_coverage"],
        }
        assert dataset["license"]["summary"] == dictionary["license"]
        assert dataset["license"]["attribution"] == dictionary["attribution"]
        assert dataset["freshness"]["data_as_of"] == PUBLISHED_AT
        assert dataset["freshness"]["last_successful_publication"] == PUBLISHED_AT
        assert dataset["freshness"]["data_as_of"] != body["generated_at"]
        for hidden in ("weather_data_status", "retrieved_at", "artifact_id", "release_id"):
            assert hidden not in dataset
            assert hidden not in dataset["coverage"]
            assert hidden not in dataset["license"]
            assert hidden not in dataset["freshness"]
        unknown_status, _unknown_headers, unknown = _request(server, "/api/v1/datasets?page=1")
        assert unknown_status == 422
        assert unknown["error"]["code"] == "UNKNOWN_QUERY_PARAMETER"
        assert unknown["error"]["details"] == {"parameter": "page"}
        assert unknown["error"]["trace_id"]
        malformed_status, _malformed_headers, malformed = _request(
            server, "/api/v1/datasets?bad=%ZZ"
        )
        assert malformed_status == 400
        assert malformed["error"]["code"] == "MALFORMED_QUERY"
        denied_status, denied_headers, denied = _request(server, "/api/v1/datasets", method="POST")
        assert denied_status == 405
        assert denied_headers["Allow"] == "GET"
        assert denied["error"]["code"] == "METHOD_NOT_ALLOWED"
        missing_status, _missing_headers, missing = _request(
            server, "/api/v1/datasets/daily-external-factors-ph"
        )
        assert missing_status == 404
        assert missing["error"]["code"] == "NOT_FOUND"
    finally:
        server.shutdown()
        server.server_close()


def test_catalog_failure_returns_503_without_the_query() -> None:
    def fail() -> list[dict]:
        raise CatalogUnavailable

    status, _headers, body = handle_catalog("GET", "/api/v1/datasets", load_rows=fail)
    assert status == 503
    assert body["error"]["code"] == "CATALOG_UNAVAILABLE"
    assert body["error"]["trace_id"]
    rendered = json.dumps(body).lower()
    assert "password" not in rendered
    assert "select" not in rendered


def test_live_catalog_lists_only_the_active_published_release() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to read the published catalog.")
    apply("dev")
    previous = active_release_id("dev")
    prior = _source_status()
    release_id = str(uuid.uuid4())
    server = catalog_server(lambda: _live_rows())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if prior != "approved":
            _set_source_status("approved")
        row = _row()
        manifest = _build(load_json(CONTRACT_PATH), [row], release_id=release_id)
        persist_manifest("dev", manifest)
        promote_release("dev", manifest, [row])
        status, _headers, body = _request(server, "/api/v1/datasets")
        assert status == 200
        listed = {item["active_release_id"] for item in body["datasets"]}
        assert listed == {release_id}
        dataset = body["datasets"][0]
        stored = _live_rows()[0]
        assert dataset["freshness"]["data_as_of"] == stored["published_at"]
        assert dataset["freshness"]["last_successful_publication"] == stored["published_at"]
        assert dataset["coverage"]["start"] == "2018-01-01"
        assert dataset["coverage"]["end"] == "2018-01-01"
        assert dataset["dataset_version"] == "1.0.0"
        suspend_source("dev", SOURCE_ID)
        after_status, _after_headers, after = _request(server, "/api/v1/datasets")
        assert after_status == 200
        assert release_id not in {item["active_release_id"] for item in after["datasets"]}
    finally:
        _set_source_status(prior)
        _cleanup([release_id])
        restore_active_release("dev", previous)
        shutil.rmtree(
            ROOT / "ops" / "local" / "runtime" / "dev" / "exports" / release_id,
            ignore_errors=True,
        )
        server.shutdown()
        server.server_close()


def _published_row() -> dict:
    return {
        "dataset_id": "daily_external_factors_ph",
        "active_release_id": RELEASE_ID,
        "dataset_version": "1.0.0",
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
        "published_at": PUBLISHED_AT,
    }


def _live_rows() -> list[dict]:
    from openfactors.api.catalog import load_catalog_rows

    return load_catalog_rows("dev")


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
