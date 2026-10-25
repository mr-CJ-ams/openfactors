"""Security checklist. The public role cannot read raw data or write."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from ops.local_stack import COMPOSE_FILE, _psql, docker_available
from ops.migrate import apply, psql
from tools.scan_secrets import find_secrets

from openfactors.api.catalog import catalog_server

ROOT = Path(__file__).resolve().parents[1]
HBA = ROOT / "ops" / "compose" / "postgres" / "pg_hba.conf"
PUBLIC_PATHS = (
    "/",
    "/docs",
    "/datasets/daily-external-factors-ph",
    "/openapi.json",
    "/api/v1/datasets",
    "/api/v1/datasets/daily-external-factors-ph",
    "/api/v1/factors/daily",
    "/api/v1/exports/daily-external-factors-ph",
)
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
HIDDEN_PATHS = (
    "/raw",
    "/staging",
    "/control",
    "/api/v1/raw",
    "/internal/secrets",
)
DENIED_SQL = (
    "SELECT * FROM staging.landed_payload;",
    "SELECT * FROM control.raw_manifests;",
    "SELECT * FROM control.ingestion_runs;",
    "SELECT * FROM core.dim_location;",
    "SELECT * FROM quality.validation_runs;",
    "INSERT INTO publish.daily_factors DEFAULT VALUES;",
    "UPDATE publish.daily_factors SET dataset_version = dataset_version;",
    "DELETE FROM publish.daily_factors;",
)


def test_http_surface_rejects_writes_hidden_paths_and_secrets() -> None:
    assert find_secrets() == []
    hba = HBA.read_text(encoding="utf-8")
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "hostnossl all all all reject" in hba
    assert "hostssl all all all scram-sha-256" in hba
    assert "host all all all trust" not in hba
    assert "127.0.0.1:${POSTGRES_PORT}:5432" in compose
    assert "127.0.0.1:${MINIO_API_PORT}:9000" in compose
    assert "https://127.0.0.1:9000/minio/health/live" in compose
    bound = catalog_server(lambda: [])
    try:
        assert bound.server_address[0] == "127.0.0.1"
    finally:
        bound.server_close()

    calls: list[str] = []
    server = catalog_server(
        lambda: calls.append("catalog") or [],
        load_release=lambda _release_id: calls.append("release") or None,
        load_factors=lambda **_kwargs: calls.append("factors") or {"rows": [], "total": 0},
        load_export=lambda **_kwargs: calls.append("export") or [],
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in PUBLIC_PATHS:
            for method in WRITE_METHODS:
                status, headers, body = _read(server, path, method)
                assert status == 405, path
                assert headers["Allow"] == "GET"
                assert body["error"]["code"] == "METHOD_NOT_ALLOWED"
                assert "secret-query" not in json.dumps(body)
        for path in HIDDEN_PATHS:
            status, _headers, body = _read(server, path)
            assert status == 404, path
            assert "secret-query" not in json.dumps(body)
        leaked, _leaked_headers, leaked_body = _read(
            server, "/api/v1/factors/daily?token=secret-query"
        )
        assert leaked == 422
        assert "secret-query" not in json.dumps(leaked_body)
        assert calls == []
        open_status, open_headers, _open_body = _read(server, "/api/v1/datasets")
        assert open_status == 200
        assert "Retry-After" not in open_headers
    finally:
        server.shutdown()
        server.server_close()

    limited = catalog_server(lambda: [], limiter=lambda _method, _path: 5)
    limited_thread = threading.Thread(target=limited.serve_forever, daemon=True)
    limited_thread.start()
    try:
        status, headers, body = _read(limited, "/api/v1/datasets")
        assert status == 429
        assert headers["Retry-After"] == "5"
        assert "secret-query" not in json.dumps(body)
    finally:
        limited.shutdown()
        limited.server_close()

    def explode() -> list[dict]:
        raise RuntimeError("secret-query")

    crashed = catalog_server(explode)
    crashed_thread = threading.Thread(target=crashed.serve_forever, daemon=True)
    crashed_thread.start()
    try:
        status, _headers, body = _read(crashed, "/")
        assert status == 500
        assert body["error"]["code"] == "INTERNAL"
        assert "secret-query" not in json.dumps(body)
        assert body["error"]["trace_id"]
    finally:
        crashed.shutdown()
        crashed.server_close()


def test_public_role_cannot_read_raw_data_or_connect_without_tls() -> None:
    if not docker_available():
        pytest.skip("Docker is required for the permission and TLS checks.")
    apply("dev")
    before = _fact_count()
    for sql in DENIED_SQL:
        denied = psql("dev", "of_api", sql)
        assert denied.returncode != 0, sql
        detail = denied.stderr.lower()
        assert "permission denied" in detail or "cannot " in detail
    plain = _psql(
        "dev",
        user="of_api",
        database="openfactors_dev",
        sslmode="disable",
        sql="SELECT 1",
    )
    assert plain.returncode != 0
    assert _fact_count() == before


def _fact_count() -> int:
    counted = psql(
        "dev",
        "of_admin",
        "SELECT count(*)::text FROM publish.daily_external_factors_ph;",
        tuples_only=True,
    )
    assert counted.returncode == 0, counted.stderr
    lines = [line.strip() for line in counted.stdout.splitlines() if line.strip()]
    return int(lines[0])


def _read(server, path: str, method: str = "GET") -> tuple[int, dict[str, str], dict | str]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
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
