"""OF-034 catalog page: geography, grain, publication, terms, and gaps."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from ops.local_stack import docker_available
from ops.migrate import apply

from openfactors.api.catalog import (
    DICTIONARY_PATH,
    CatalogUnavailable,
    catalog_server,
    load_catalog_rows,
)
from openfactors.api.catalog_page import handle_catalog_page

RELEASE_ID = "00000000-0000-4000-8000-000000000340"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"
GENERATED_AT = "2026-10-25T01:00:00Z"
FAILED_AT = "1999-01-01T00:00:00Z"
GAP = "A missing weather measure stays null."


def test_catalog_page_shows_publication_terms_and_gaps() -> None:
    dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    row = _published_row()
    row["last_failed_attempt"] = FAILED_AT
    server = _server(lambda: [row])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _read(server, "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "<h1>OpenFactors</h1>" in body
        assert 'id="catalog"' in body
        assert "not proof of causation" in body
        article = body.split("<article>", 1)[1].split("</article>", 1)[0]
        assert dictionary["dataset_id"] in article
        assert dictionary["grain"] in article
        assert dictionary["geographic_coverage"] in article
        assert "2018-01-01" in article
        assert "Last successful publication" in article
        assert PUBLISHED_AT in article
        assert dictionary["license"] in article
        assert dictionary["attribution"] in article
        assert "Weather data by Open-Meteo.com" in article
        assert 'href="https://open-meteo.com/"' in article
        holiday = "National holidays as proclaimed in the Official Gazette of the Philippines"
        assert holiday in article
        assert GAP in article
        assert FAILED_AT not in body
        assert "last failed" not in body.lower()
        assert "retrieved_at" not in body
        assert _time_after(article, "Last successful publication") == PUBLISHED_AT
        generated = _time_after(body, "Catalog response generated")
        assert generated != PUBLISHED_AT
        assert generated not in article
    finally:
        server.shutdown()
        server.server_close()


def test_gap_text_is_escaped_and_is_not_a_publication_time() -> None:
    dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    dictionary["known_gaps"] = "<script>alert(1)</script>"
    row = _published_row()
    row["last_failed_attempt"] = FAILED_AT
    status, headers, payload = handle_catalog_page(
        "GET",
        "/",
        load_rows=lambda: [row],
        dictionary=dictionary,
        generated_at=GENERATED_AT,
    )
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    body = payload.decode("utf-8")
    article = body.split("<article>", 1)[1].split("</article>", 1)[0]
    assert "<script>alert" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in article
    assert FAILED_AT not in body
    assert _time_after(article, "Last successful publication") == PUBLISHED_AT
    assert _time_after(body, "Catalog response generated") == GENERATED_AT


def test_empty_and_unavailable_pages_do_not_invent_a_publication() -> None:
    empty = _server(lambda: [])
    failed = _server(_fail)
    empty_thread = threading.Thread(target=empty.serve_forever, daemon=True)
    failed_thread = threading.Thread(target=failed.serve_forever, daemon=True)
    empty_thread.start()
    failed_thread.start()
    try:
        status, _headers, body = _read(empty, "/")
        assert status == 200
        assert "No published dataset is available." in body
        assert "<article>" not in body
        assert "Last successful publication" not in body
        assert RELEASE_ID not in body
        assert GAP not in body
        down, down_headers, down_body = _read(failed, "/")
        assert down == 503
        assert down_headers["Content-Type"].startswith("text/html")
        assert "The catalog is temporarily unavailable." in down_body
        assert "Last successful publication" not in down_body
        assert "<article>" not in down_body
    finally:
        empty.shutdown()
        empty.server_close()
        failed.shutdown()
        failed.server_close()


def test_catalog_page_rejects_a_bad_request_without_a_dataset() -> None:
    server = _server(lambda: [_published_row()])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        unknown_status, _unknown_headers, unknown = _read(server, "/?page=1", json_body=True)
        assert unknown_status == 422
        assert unknown["error"]["code"] == "UNKNOWN_QUERY_PARAMETER"
        denied_status, denied_headers, denied = _read(server, "/", method="POST", json_body=True)
        assert denied_status == 405
        assert denied_headers["Allow"] == "GET"
        assert denied["error"]["code"] == "METHOD_NOT_ALLOWED"
        crashed = _server(_explode)
        crashed_thread = threading.Thread(target=crashed.serve_forever, daemon=True)
        crashed_thread.start()
        try:
            status, _headers, body = _read(crashed, "/", json_body=True)
        finally:
            crashed.shutdown()
            crashed.server_close()
        assert status == 500
        assert body["error"]["code"] == "INTERNAL"
        assert body["error"]["message"] == "The catalog request failed."
        assert "secret-query" not in json.dumps(body)
    finally:
        server.shutdown()
        server.server_close()


def test_live_catalog_page_matches_the_published_list() -> None:
    if not docker_available():
        pytest.skip("Docker is required to read the published catalog.")
    apply("dev")
    server = catalog_server(load_catalog_rows_dev, load_release=lambda _release_id: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        page_status, page_headers, page = _read(server, "/")
        api_status, _api_headers, api = _read(server, "/api/v1/datasets", json_body=True)
        assert page_status == api_status == 200
        assert page_headers["Content-Type"].startswith("text/html")
        assert "last failed" not in page.lower()
        if not api["datasets"]:
            assert "No published dataset is available." in page
            return
        dataset = api["datasets"][0]
        assert dataset["grain"] in page
        assert dataset["coverage"]["geographic"] in page
        assert dataset["freshness"]["last_successful_publication"] in page
        assert dataset["license"]["attribution"] in page
        assert "Last successful publication" in page
        assert GAP in page
    finally:
        server.shutdown()
        server.server_close()


def _server(load_rows):
    return catalog_server(load_rows, load_release=lambda _release_id: None)


def _time_after(body: str, label: str) -> str:
    chunk = body.split(label, 1)[1]
    return chunk.split('datetime="', 1)[1].split('"', 1)[0]


def _published_row() -> dict:
    return {
        "dataset_id": "daily_external_factors_ph",
        "active_release_id": RELEASE_ID,
        "dataset_version": "1.0.0",
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
        "published_at": PUBLISHED_AT,
    }


def _fail() -> list[dict]:
    raise CatalogUnavailable


def _explode() -> list[dict]:
    raise RuntimeError("secret-query")


def load_catalog_rows_dev() -> list[dict]:
    return load_catalog_rows("dev")


def _read(
    server: ThreadingHTTPServer,
    path: str,
    method: str = "GET",
    *,
    json_body: bool = False,
) -> tuple[int, dict[str, str], str | dict]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode("utf-8")
            if json_body:
                payload = json.loads(payload)
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8")
        if json_body or error.headers.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload)
        return error.code, dict(error.headers), payload
