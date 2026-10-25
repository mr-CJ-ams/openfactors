"""The operator backfill form stays off the public API."""

import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import pytest

from openfactors.api.catalog import catalog_server
from openfactors.api.openapi import implementation_schema
from openfactors.ingestion.errors import ConnectorError
from openfactors.orchestration.backfill_ui import (
    handle_operator,
    main,
    operator_server,
    render_form,
    request_from_fields,
    submit_form,
)

NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
FIELDS = {
    "source": ["open-meteo-era5"],
    "location_id": ["ph-manila"],
    "start_date": ["2018-01-01"],
    "end_date": ["2018-01-02"],
    "operator": ["maintainer"],
    "reason": ["sample gap"],
}


def test_form_is_local_and_dry_run_is_the_default() -> None:
    page = render_form()
    assert "This form is not the public API." in page
    assert "open-meteo-era5" in page
    assert "ph-official-gazette" in page
    assert "ph-manila" in page
    assert 'value="all"' in page
    assert 'name="dry_run" type="checkbox" value="on" checked' in page
    assert "PH-PILOT" not in page
    request = request_from_fields(FIELDS)
    assert request.dry_run is True
    live = dict(FIELDS)
    live["dry_run"] = ["off"]
    assert request_from_fields(live).dry_run is False
    calls: list[str] = []

    def step(_scope: object) -> None:
        calls.append("called")

    request, record = submit_form(FIELDS, now=NOW, steps={"weather_fetch": step})
    assert request.dry_run is True
    assert record.status == "succeeded"
    assert calls == []


def test_anonymous_operator_is_refused() -> None:
    fields = dict(FIELDS)
    fields["operator"] = ["anonymous"]
    with pytest.raises(ConnectorError, match="A backfill needs a named operator."):
        submit_form(fields, now=NOW)
    status, _headers, body = handle_operator(
        "POST",
        "/",
        urllib.parse.urlencode({key: values[0] for key, values in fields.items()}).encode(),
        now=NOW,
    )
    assert status == 422
    assert "A backfill needs a named operator." in body.decode("utf-8")


def test_public_api_has_no_backfill_route() -> None:
    paths = implementation_schema()["paths"]
    assert all("backfill" not in path for path in paths)
    server = catalog_server(lambda: [])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/backfill", "/api/v1/backfill"):
            for method in ("GET", "POST"):
                status, _headers, body = _read(server, path, method)
                assert status == 404
                assert "This form is not the public API." not in json.dumps(body)
    finally:
        server.shutdown()
        server.server_close()


def test_operator_server_stays_on_this_computer() -> None:
    with pytest.raises(ConnectorError, match="listens on this computer"):
        operator_server(host="0.0.0.0")
    server = operator_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        status, _headers, page = _read(server, "/")
        assert status == 200
        assert "This form is not the public API." in page
        hidden, _hidden_headers, hidden_body = _read(server, "/api/v1/factors/daily")
        assert hidden == 404
        assert "temperature_mean_c" not in hidden_body
        posted, _posted_headers, posted_body = _read(
            server,
            "/",
            "POST",
            urllib.parse.urlencode({key: values[0] for key, values in FIELDS.items()}).encode(),
        )
        assert posted == 200
        assert "Dry-run accepted." in posted_body
        assert "ph-manila" in posted_body
    finally:
        server.shutdown()
        server.server_close()


def test_operator_command_prints_the_form() -> None:
    out = io.StringIO()
    assert main([], out) == 0
    assert "This form is not the public API." in out.getvalue()
    with pytest.raises(SystemExit, match="only --serve"):
        main(["--host", "0.0.0.0"])


def _read(
    server,
    path: str,
    method: str = "GET",
    data: bytes | None = None,
) -> tuple[int, dict[str, str], str | dict]:
    _host, port = server.server_address
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
    )
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
