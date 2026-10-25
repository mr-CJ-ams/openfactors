"""OF-036 API guide: documented routes and executable examples."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tests.test_release_manifest import _row

from openfactors.api.api_guide import GUIDE_PATH, PAGE_PATH, example_requests, load_guide
from openfactors.api.catalog import catalog_server
from openfactors.api.csv_export import EXPORT_PATH

PIN = "00000000-0000-4000-8000-000000000360"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"


def test_guide_documents_routes_limits_and_has_no_credentials() -> None:
    text = load_guide()
    assert text == GUIDE_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    for path in (
        "/api/v1/datasets",
        "/api/v1/datasets/daily-external-factors-ph",
        "/api/v1/factors/daily",
        "/api/v1/exports/daily-external-factors-ph",
        "/openapi.json",
    ):
        assert path in text
    assert "Authentication is not required." in text
    assert "100,000" in text
    assert "50,000,000" in text
    assert "1 to 1000" in text
    assert "No date-span maximum is set." in text
    assert "No request quota is set." in text
    assert "That release is not available." in text
    assert "start_date must be on or before end_date" in text
    assert "Retry-After" in text
    assert "trace_id" in text
    lowered = text.lower()
    assert "authorization" not in lowered
    assert "bearer" not in lowered
    assert "api-key" not in lowered
    examples = example_requests(text)
    assert len(examples) == 7
    assert {status for status, _url, _command in examples} == {200, 422}


def test_example_requests_run_against_the_seeded_api() -> None:
    server = catalog_server(
        lambda: [_published()],
        load_release=_release,
        load_factors=_factors,
        load_export=lambda **_filters: [_row()],
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home_status, _home_headers, home = _read(server, "/")
        guide_status, guide_headers, guide = _read(server, PAGE_PATH)
        assert home_status == guide_status == 200
        assert 'href="/docs"' in home
        assert guide_headers["Content-Type"].startswith("text/html")
        assert "Authentication is not required." in guide
        assert "/api/v1/factors/daily" in guide
        pinned = ""
        for expected, url, command in example_requests(load_guide()):
            assert command.startswith("curl ")
            status, headers, body = _read(server, _request_path(url))
            assert status == expected
            path = urlsplit(url).path
            if path.endswith("/factors/daily") and expected == 200:
                assert body["release_id"] == headers["X-Release-Id"] == PIN
                assert body["pagination"]["page_size"] == 100
                if "page=2" in url:
                    assert body["data"] == []
                    assert body["pagination"]["page"] == 2
                    assert body["pagination"]["total"] == 1
                else:
                    assert body["data"][0]["date"] == "2018-01-01"
                    assert body["pagination"]["page"] == 1
                pinned = body["release_id"]
            elif path == "/api/v1/datasets" and expected == 200:
                assert body["datasets"][0]["dataset_id"] == "daily_external_factors_ph"
            elif path.endswith("daily-external-factors-ph") and "export" not in path:
                assert len(body["columns"]) == 12
                assert body["release"]["release_id"] == PIN
            elif path == "/openapi.json":
                assert body["openapi"] == "3.1.0"
            elif path == EXPORT_PATH:
                assert headers["Content-Type"].startswith("text/csv")
                assert headers["X-Release-Id"] == PIN
                assert body.startswith(b"date,")
            elif expected == 422:
                assert body["error"]["code"] == "INVALID_DATE_RANGE"
                assert body["error"]["message"] == "start_date must be on or before end_date"
                assert body["error"]["trace_id"]
        assert pinned == PIN
        denied_status, denied_headers, denied = _read(
            server, PAGE_PATH, method="POST", json_body=True
        )
        assert denied_status == 405
        assert denied_headers["Allow"] == "GET"
        assert denied["error"]["code"] == "METHOD_NOT_ALLOWED"
    finally:
        server.shutdown()
        server.server_close()


def _published() -> dict:
    return {
        "dataset_id": "daily_external_factors_ph",
        "active_release_id": PIN,
        "dataset_version": "1.0.0",
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
        "published_at": PUBLISHED_AT,
    }


def _release(release_id: str | None) -> dict | None:
    if release_id not in (None, PIN):
        return None
    return {
        "dataset_id": "daily_external_factors_ph",
        "release_id": PIN,
        "dataset_version": "1.0.0",
        "published_at": PUBLISHED_AT,
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
    }


def _factors(**filters: object) -> dict:
    if filters["page"] == 2:
        return {"total": 1, "rows": []}
    return {"total": 1, "rows": [_row()]}


def _request_path(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert "release_id" not in query or query["release_id"] == [PIN]
    suffix = f"?{parts.query}" if parts.query else ""
    return f"{parts.path}{suffix}"


def _read(
    server: ThreadingHTTPServer,
    path: str,
    method: str = "GET",
    *,
    json_body: bool = False,
) -> tuple[int, dict[str, str], dict | str | bytes]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return _payload(response.status, response.headers, response.read(), json_body)
    except urllib.error.HTTPError as error:
        return _payload(error.code, error.headers, error.read(), json_body)


def _payload(status: int, headers, raw: bytes, json_body: bool):
    content_type = headers.get("Content-Type", "")
    if json_body or content_type.startswith("application/json"):
        body = json.loads(raw.decode("utf-8"))
    elif "text/csv" in content_type:
        body = raw
    else:
        body = raw.decode("utf-8")
    return status, dict(headers), body
