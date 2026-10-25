"""OF-030 OpenAPI lint, implementation diff, and GET /openapi.json."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

from openfactors.api.catalog import (
    CATALOG_PATH,
    DATASET_PATH,
    CatalogUnavailable,
    catalog_server,
)
from openfactors.api.csv_export import EXPORT_PATH
from openfactors.api.factors import FACTORS_PATH
from openfactors.api.openapi import (
    _RESPONSES,
    OPENAPI_ROUTE,
    check,
    compare,
    implementation_schema,
    lint,
    load_approved,
    load_diffs,
)

_LEAK = "Traceback (most recent call last): password SELECT"
_PIN = "00000000-0000-4000-8000-000000000300"
_FACTORS = f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01"
_EXPORT = (
    f"{EXPORT_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01&format=csv"
)


def test_approved_yaml_matches_the_generated_route_schema() -> None:
    assert check() == []
    approved = load_approved()
    diffs = load_diffs()
    implemented = implementation_schema()
    hidden_gap = {**diffs, "response_gaps": []}
    catalog_gap = compare(implemented, approved, hidden_gap)
    export_path = "/api/v1/exports/daily-external-factors-ph"
    csv_only = {
        "paths": {
            **implemented["paths"],
            export_path: {
                **implemented["paths"][export_path],
                "content_types": {"text/csv"},
            },
        }
    }
    export_gap = compare(csv_only, approved, diffs)
    assert f"content:{export_path}" in export_gap
    assert "responses:/api/v1/datasets" in catalog_gap
    broken = load_approved()
    broken["paths"][CATALOG_PATH]["get"]["responses"]["200"] = {
        "description": "broken",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Missing"}}},
    }
    assert any(item.startswith("ref:") for item in lint(broken))
    assert OPENAPI_ROUTE not in approved["paths"]
    assert approved["openapi"] == "3.1.0"


def test_openapi_route_serves_the_document_and_the_declared_statuses() -> None:
    observed = {path: set() for path in _RESPONSES}
    servers = [
        _serve(
            catalog_server(
                lambda: [],
                load_release=_release,
                load_factors=lambda **_filters: {"total": 0, "rows": []},
                load_export=lambda **_filters: [],
            )
        ),
        _serve(catalog_server(lambda: [], limiter=lambda _method, _path: 9)),
        _serve(
            catalog_server(
                _unavailable,
                load_release=_unavailable,
                load_factors=_unavailable,
                load_export=_unavailable,
            )
        ),
        _serve(
            catalog_server(
                _explode,
                load_release=_explode,
                load_factors=_explode,
                load_export=_explode,
            )
        ),
    ]
    ordinary, limited, unavailable, exploded = servers
    try:
        _record(observed, ordinary, "GET", CATALOG_PATH, 200)
        _record(observed, ordinary, "GET", f"{CATALOG_PATH}?%", 400)
        _record(observed, ordinary, "POST", CATALOG_PATH, 405)
        _record(observed, ordinary, "GET", f"{CATALOG_PATH}?page=1", 422)
        _record(observed, ordinary, "GET", f"{DATASET_PATH}?release_id={_PIN}", 200)
        _record(observed, ordinary, "GET", f"{DATASET_PATH}?%", 400)
        _record(observed, ordinary, "GET", f"{DATASET_PATH}?release_id={uuid.uuid4()}", 404)
        _record(observed, ordinary, "GET", f"{DATASET_PATH}?page=1", 422)
        _record(observed, ordinary, "POST", DATASET_PATH, 405)
        _record(observed, ordinary, "GET", _FACTORS, 200)
        _record(observed, ordinary, "GET", f"{FACTORS_PATH}?%", 400)
        _record(observed, ordinary, "GET", FACTORS_PATH, 422)
        _record(
            observed,
            ordinary,
            "GET",
            f"{FACTORS_PATH}?location_id=ph-nowhere&start_date=2018-01-01&end_date=2018-01-01",
            404,
        )
        _record(observed, ordinary, "POST", FACTORS_PATH, 405)
        _record(observed, ordinary, "GET", _EXPORT, 200)
        _record(observed, ordinary, "GET", f"{EXPORT_PATH}?%", 400)
        _record(
            observed,
            ordinary,
            "GET",
            f"{EXPORT_PATH}?location_id=ph-manila&start_date=2018-01-01"
            "&end_date=2018-01-01&format=parquet",
            200,
        )
        _record(
            observed,
            ordinary,
            "GET",
            f"{EXPORT_PATH}?location_id=ph-manila&start_date=2018-01-01"
            "&end_date=2018-01-01&format=xml",
            422,
        )
        _record(
            observed,
            ordinary,
            "GET",
            f"{EXPORT_PATH}?location_id=ph-nowhere&start_date=2018-01-01"
            "&end_date=2018-01-01&format=csv",
            404,
        )
        _record(observed, ordinary, "POST", EXPORT_PATH, 405)
        status, _headers, body = _request(ordinary, OPENAPI_ROUTE)
        assert status == 200
        assert body == load_approved()
        _record(observed, ordinary, "GET", f"{OPENAPI_ROUTE}?%", 400)
        _record(observed, ordinary, "GET", f"{OPENAPI_ROUTE}?page=1", 422)
        _record(observed, ordinary, "POST", OPENAPI_ROUTE, 405)
        for path in (CATALOG_PATH, DATASET_PATH, _FACTORS, _EXPORT, OPENAPI_ROUTE):
            limited_status, headers, payload = _request(limited, path)
            assert limited_status == 429
            assert headers["Retry-After"] == "9"
            assert payload["error"]["code"] == "RATE_LIMITED"
            if path.split("?", 1)[0] in observed:
                observed[path.split("?", 1)[0]].add("429")
        for server, code in ((unavailable, "503"), (exploded, "500")):
            for path in (CATALOG_PATH, DATASET_PATH, _FACTORS, _EXPORT):
                found, _headers, payload = _request(server, path)
                assert found == int(code)
                assert _LEAK.lower() not in json.dumps(payload).lower()
                observed[path.split("?", 1)[0]].add(code)
        assert observed == {path: set(codes) for path, codes in _RESPONSES.items()}
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _release(release_id: str | None) -> dict | None:
    if release_id not in (None, _PIN):
        return None
    return {
        "dataset_id": "daily_external_factors_ph",
        "dataset_version": "1.0.0",
        "release_id": _PIN,
        "published_at": "2026-10-25T00:00:00Z",
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
    }


def _explode(*_args: object, **_kwargs: object) -> dict:
    raise RuntimeError(_LEAK)


def _unavailable(*_args: object, **_kwargs: object) -> dict:
    raise CatalogUnavailable(_LEAK)


def _record(
    observed: dict[str, set[str]],
    server: ThreadingHTTPServer,
    method: str,
    path: str,
    status: int,
) -> None:
    found, _headers, _body = _request(server, path, method)
    assert found == status, path
    route = path.split("?", 1)[0]
    if route in observed:
        observed[route].add(str(status))


def _serve(server: ThreadingHTTPServer) -> ThreadingHTTPServer:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _request(
    server: ThreadingHTTPServer, path: str, method: str = "GET"
) -> tuple[int, dict[str, str], dict]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            binary = "text/csv" in content_type or "parquet" in content_type
            payload = {} if binary else json.loads(raw.decode("utf-8"))
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
        return error.code, dict(error.headers), payload
