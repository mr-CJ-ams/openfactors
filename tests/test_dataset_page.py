"""OF-035 dictionary, inline filter errors, and a pinned download link."""

from __future__ import annotations

import html
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, field_names, load_json
from ops.local_stack import dev_database_ready
from ops.migrate import apply

from openfactors.api.catalog import (
    DICTIONARY_PATH,
    CatalogUnavailable,
    catalog_server,
    load_accessible_release,
)
from openfactors.api.csv_export import EXPORT_PATH
from openfactors.api.dataset_page import PAGE_PATH, handle_dataset_page

ACTIVE = "00000000-0000-4000-8000-000000000351"
OLDER = "00000000-0000-4000-8000-000000000352"
PUBLISHED_AT = "2026-10-24T16:00:00+00:00"
GENERATED_AT = "2026-10-25T01:00:00Z"
VALID = (
    f"{PAGE_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-02&format=parquet"
)


def test_dictionary_is_labeled_and_the_catalog_links_to_it() -> None:
    dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    server = _server(_release)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home_status, _home_headers, home = _read(server, "/")
        status, headers, body = _read(server, PAGE_PATH)
        assert home_status == status == 200
        assert "Dictionary and download" in home
        assert f'href="{PAGE_PATH}"' in home
        assert headers["Content-Type"].startswith("text/html")
        assert [column["name"] for column in dictionary["columns"]] == field_names(
            load_json(CONTRACT_PATH)
        )
        assert body.count('<th scope="row">') == 12
        for column in dictionary["columns"]:
            assert column["name"] in body
            assert html.escape(column["logical_type"], quote=True) in body
            assert html.escape(column["description"], quote=True) in body
            assert html.escape(column["source"], quote=True) in body
        assert "Nullable" in body
        assert "°C" in body
        for name in ("location_id", "start_date", "end_date", "release_id"):
            assert f'for="{name}"' in body
            assert f'id="{name}"' in body
        assert 'id="format_csv"' in body
        assert 'id="format_parquet"' in body
        assert "tabindex" not in body
        assert f'value="{ACTIVE}"' in body
        assert "Download CSV" not in body
        assert "Download Parquet" not in body
        assert "Review download" in body
    finally:
        server.shutdown()
        server.server_close()


def test_invalid_filters_stay_on_the_page() -> None:
    server = _server(_release)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _status, _headers, body = _read(
            server,
            f"{PAGE_PATH}?location_id=ph-manila&start_date=2018-01-02"
            "&end_date=2018-01-01&format=csv",
        )
        assert "start_date must be on or before end_date" in body
        assert 'id="end_date_error"' in body
        assert 'aria-invalid="true"' in body
        assert 'href="#end_date"' in body
        assert "autofocus" in body
        assert EXPORT_PATH not in body
        missing, _missing_headers, missing_body = _read(
            server,
            f"{PAGE_PATH}?start_date=2018-01-01&end_date=2018-01-02&format=csv",
        )
        assert missing == 200
        assert "location_id is required." in missing_body
        assert EXPORT_PATH not in missing_body
        unknown, _unknown_headers, unknown_body = _read(
            server,
            f"{VALID}&location_id=no-such-city".replace("location_id=ph-manila&", ""),
        )
        assert "That location is not available." in unknown_body
        assert EXPORT_PATH not in unknown_body
        bad_pin, _pin_headers, pin_body = _read(server, f"{VALID}&release_id=not-a-uuid")
        assert bad_pin == 200
        assert "release_id must be a UUID." in pin_body
        assert EXPORT_PATH not in pin_body
    finally:
        server.shutdown()
        server.server_close()


def test_download_link_shows_the_same_pinned_release() -> None:
    seen: list[str | None] = []

    def load(release_id: str | None) -> dict | None:
        seen.append(release_id)
        if release_id in (None, ACTIVE):
            return _stored(ACTIVE)
        if release_id == OLDER:
            return _stored(OLDER)
        return None

    server = _server(load)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _status, _headers, active = _read(server, VALID)
        assert f'<code id="pinned_release">{ACTIVE}</code>' in active
        assert f"release_id={ACTIVE}" in active
        assert f"{EXPORT_PATH}?" in active
        assert "Download Parquet" in active
        assert "Download CSV" not in active
        assert f"for release {ACTIVE}" in active
        _older_status, _older_headers, older = _read(server, f"{VALID}&release_id={OLDER.upper()}")
        assert OLDER in seen
        assert f'<code id="pinned_release">{OLDER}</code>' in older
        assert f"release_id={OLDER}" in older
        assert ACTIVE not in older
        assert "Last successful publication" in older
        assert PUBLISHED_AT in older.split("Last successful publication", 1)[1]
    finally:
        server.shutdown()
        server.server_close()


def test_field_text_is_escaped_and_failures_add_no_release() -> None:
    dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    dictionary["columns"][0]["description"] = "<script>alert(1)</script>"
    status, _headers, payload = handle_dataset_page(
        "GET",
        PAGE_PATH,
        load_release=_release,
        dictionary=dictionary,
        generated_at=GENERATED_AT,
    )
    assert status == 200
    body = payload.decode("utf-8")
    assert "<script>alert" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    failed = _server(_explode)
    down = _server(_fail)
    failed_thread = threading.Thread(target=failed.serve_forever, daemon=True)
    down_thread = threading.Thread(target=down.serve_forever, daemon=True)
    failed_thread.start()
    down_thread.start()
    try:
        error_status, _error_headers, error = _read(failed, PAGE_PATH, json_body=True)
        denied_status, denied_headers, denied = _read(
            failed, PAGE_PATH, method="POST", json_body=True
        )
        unknown_status, _unknown_headers, unknown = _read(
            failed, f"{PAGE_PATH}?page=1", json_body=True
        )
        unavailable, unavailable_headers, unavailable_body = _read(down, PAGE_PATH)
    finally:
        failed.shutdown()
        failed.server_close()
        down.shutdown()
        down.server_close()
    assert error_status == 500
    assert error["error"]["message"] == "The dictionary request failed."
    assert "secret-query" not in json.dumps(error)
    assert denied_status == 405
    assert denied_headers["Allow"] == "GET"
    assert denied["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert unknown_status == 422
    assert unknown["error"]["code"] == "UNKNOWN_QUERY_PARAMETER"
    assert unavailable == 503
    assert unavailable_headers["Content-Type"].startswith("text/html")
    assert "The dictionary is temporarily unavailable." in unavailable_body
    assert "pinned_release" not in unavailable_body


def test_live_dictionary_page_lists_the_public_fields() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to read the published release.")
    apply("dev")
    server = catalog_server(
        lambda: [],
        load_release=lambda release_id: load_accessible_release("dev", release_id),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = _read(server, PAGE_PATH)
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert body.count('<th scope="row">') == 12
        assert "temperature_mean_c" in body
        assert 'for="location_id"' in body
        assert "last failed" not in body.lower()
    finally:
        server.shutdown()
        server.server_close()


def _server(load_release):
    return catalog_server(lambda: [_published()], load_release=load_release)


def _published() -> dict:
    return {
        "dataset_id": "daily_external_factors_ph",
        "active_release_id": ACTIVE,
        "dataset_version": "1.0.0",
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
        "published_at": PUBLISHED_AT,
    }


def _stored(release_id: str) -> dict:
    return {
        "dataset_id": "daily_external_factors_ph",
        "release_id": release_id,
        "dataset_version": "1.0.0",
        "published_at": PUBLISHED_AT,
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
    }


def _release(release_id: str | None) -> dict | None:
    if release_id in (None, ACTIVE):
        return _stored(ACTIVE)
    return None


def _fail(_release_id: str | None) -> dict | None:
    raise CatalogUnavailable


def _explode(_release_id: str | None) -> dict | None:
    raise RuntimeError("secret-query")


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
