"""Accounts and saved workspaces are absent from the public routes."""

import json
import threading
import urllib.error
import urllib.request

from openfactors.api.catalog import CATALOG_PATH, DATASET_PATH, catalog_server
from openfactors.api.csv_export import EXPORT_PATH
from openfactors.api.factors import FACTORS_PATH
from openfactors.api.openapi import implementation_schema

EXCLUDED = (
    "/login",
    "/account",
    "/accounts",
    "/signup",
    "/register",
    "/workspace",
    "/workspaces",
    "/api/v1/accounts",
    "/api/v1/workspaces",
)
PAGES = ("/", "/docs", "/datasets/daily-external-factors-ph")
CONTROLS = ('type="password"', "sign in", "log in", "create account", 'href="/login"')


def test_public_routes_have_no_account_surface() -> None:
    assert set(implementation_schema()["paths"]) == {
        CATALOG_PATH,
        DATASET_PATH,
        FACTORS_PATH,
        EXPORT_PATH,
    }
    server = catalog_server(lambda: [])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in PAGES:
            status, headers, body = _read(server, path)
            assert status == 200
            assert "Set-Cookie" not in headers
            lowered = body.lower()
            for control in CONTROLS:
                assert control not in lowered
        for path in EXCLUDED:
            for method in ("GET", "POST"):
                status, headers, body = _read(server, path, method)
                assert status == 404
                assert "Set-Cookie" not in headers
                assert "password" not in json.dumps(body).lower()
    finally:
        server.shutdown()
        server.server_close()


def _read(server, path: str, method: str = "GET") -> tuple[int, dict[str, str], str | dict]:
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
