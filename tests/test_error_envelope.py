"""OF-029 negative checks for the shared error envelope."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

from openfactors.api.catalog import CATALOG_PATH, DATASET_PATH, CatalogUnavailable, catalog_server
from openfactors.api.factors import FACTORS_PATH

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"
_TRACE = re.compile(r"^[0-9a-f]{32}$")
_LEAK = "Traceback (most recent call last): password SELECT RuntimeError"
_STATUSES = ("400", "404", "422", "429", "500", "503")
FACTORS = f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01"


def test_openapi_declares_one_error_schema_for_every_route() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["Error"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["error"]
    assert schema["properties"]["error"]["required"] == ["code", "message", "trace_id"]
    for path, operations in document["paths"].items():
        responses = operations["get"]["responses"]
        for status in _STATUSES:
            body = responses[status]["content"]["application/json"]["schema"]
            assert body["$ref"].endswith("/Error"), path
        assert "Retry-After" in responses["429"]["headers"]


def test_client_errors_use_the_envelope_and_hide_withdrawal() -> None:
    server = _serve(
        catalog_server(
            lambda: [],
            load_release=lambda _release_id: None,
            load_factors=lambda **_filters: (_ for _ in ()).throw(RuntimeError(_LEAK)),
        )
    )
    try:
        malformed = _request(server, f"{CATALOG_PATH}?%")
        invalid = _request(server, f"{CATALOG_PATH}?page=1")
        method = _request(server, CATALOG_PATH, method="POST")
        unknown = _request(server, f"{DATASET_PATH}?release_id={uuid.uuid4()}")
        withdrawn = _request(server, f"{DATASET_PATH}?release_id={uuid.uuid4()}")
        location = _request(
            server,
            f"{FACTORS_PATH}?location_id=ph-nowhere&start_date=2018-01-01&end_date=2018-01-01",
        )
        assert malformed[0] == 400
        assert invalid[0] == 422
        assert method[0] == 405
        assert method[1]["Allow"] == "GET"
        assert unknown[0] == withdrawn[0] == location[0] == 404
        traces = [
            _assert_envelope(malformed[2], "MALFORMED_QUERY"),
            _assert_envelope(invalid[2], "UNKNOWN_QUERY_PARAMETER"),
            _assert_envelope(method[2], "METHOD_NOT_ALLOWED"),
            _assert_envelope(unknown[2], "NOT_FOUND"),
            _assert_envelope(withdrawn[2], "NOT_FOUND"),
            _assert_envelope(location[2], "NOT_FOUND"),
        ]
        assert len(set(traces)) == len(traces)
        assert location[2]["error"]["message"] == "That location is not available."
        assert unknown[2]["error"]["message"] == withdrawn[2]["error"]["message"]
        assert "withdrawn" not in unknown[2]["error"]["message"].lower()
        assert isinstance(invalid[2]["error"]["details"], dict)
    finally:
        _close(server)


def test_limiter_returns_429_without_a_configured_quota() -> None:
    def allow(method: str, path: str) -> None:
        assert method == "GET"
        assert path == CATALOG_PATH
        return None

    open_server = _serve(catalog_server(lambda: [], limiter=allow))
    limited = _serve(
        catalog_server(
            lambda: (_ for _ in ()).throw(RuntimeError(_LEAK)),
            limiter=lambda _method, _path: 12,
        )
    )

    def fail_closed(_method: str, _path: str) -> int:
        raise RuntimeError(_LEAK)

    broken = _serve(catalog_server(lambda: [], limiter=fail_closed))
    try:
        allowed = _request(open_server, CATALOG_PATH)
        blocked = _request(limited, CATALOG_PATH)
        failed = _request(broken, CATALOG_PATH)
        assert allowed[0] == 200
        assert "error" not in allowed[2]
        assert blocked[0] == 429
        assert int(blocked[1]["Retry-After"]) == 12
        _assert_envelope(blocked[2], "RATE_LIMITED")
        assert failed[0] == 500
        assert "Retry-After" not in failed[1]
        _assert_envelope(failed[2], "INTERNAL")
    finally:
        _close(open_server)
        _close(limited)
        _close(broken)


def test_dependency_and_internal_failures_omit_the_exception() -> None:
    def explode_release(_release_id: str | None) -> None:
        raise CatalogUnavailable(_LEAK)

    server = _serve(
        catalog_server(
            lambda: (_ for _ in ()).throw(RuntimeError(_LEAK)),
            load_release=explode_release,
            load_factors=lambda **_filters: (_ for _ in ()).throw(RuntimeError(_LEAK)),
        )
    )
    try:
        internal = _request(server, CATALOG_PATH)
        catalog = _request(server, DATASET_PATH)
        factors = _request(server, FACTORS)
        assert internal[0] == 500
        assert catalog[0] == factors[0] == 503
        first = _assert_envelope(internal[2], "INTERNAL")
        second = _assert_envelope(catalog[2], "CATALOG_UNAVAILABLE")
        third = _assert_envelope(factors[2], "FACTORS_UNAVAILABLE")
        assert len({first, second, third}) == 3
    finally:
        _close(server)


def _assert_envelope(body: dict, code: str) -> str:
    assert set(body) == {"error"}
    error = body["error"]
    allowed = {"code", "message", "trace_id", "details"}
    assert {"code", "message", "trace_id"} <= set(error) <= allowed
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert _TRACE.fullmatch(error["trace_id"])
    if "details" in error:
        assert isinstance(error["details"], dict)
    encoded = json.dumps(body).lower()
    for leak in ("traceback", "password", "select", "runtimeerror", "catalogunavailable"):
        assert leak not in encoded
    return error["trace_id"]


def _serve(server: ThreadingHTTPServer) -> ThreadingHTTPServer:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _close(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


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
