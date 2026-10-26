"""The local catalog command reads the published release on this computer."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal

import pytest
from ops.local_stack import dev_database_ready
from tests.test_example_joins import published_history_fixture

from openfactors.api.catalog import main, open_catalog

FACTORS = "/api/v1/factors/daily?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01"


def test_catalog_command_stays_on_this_computer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENFACTORS_API_BIND_HOST", raising=False)
    monkeypatch.delenv("OPENFACTORS_API_BIND_PORT", raising=False)
    monkeypatch.delenv("OPENFACTORS_API_PGHOST", raising=False)
    with pytest.raises(SystemExit, match="no arguments"):
        main(["--host", "0.0.0.0"])
    monkeypatch.setattr("ops.local_stack.dev_database_ready", lambda: False)
    with pytest.raises(SystemExit, match="not running"):
        main([])


def test_local_catalog_serves_the_published_manila_day() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to serve the local catalog.")
    with published_history_fixture() as release_id:
        server = open_catalog("dev", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert server.server_address[0] == "127.0.0.1"
            status, headers, body = _read(server, FACTORS)
            assert status == 200
            assert headers["X-Dataset-Version"] == "1.0.0"
            assert body["pagination"]["total"] == 1
            assert body["release_id"] == release_id
            row = body["data"][0]
            assert row["location_id"] == "ph-manila"
            assert row["date"] == "2018-01-01"
            assert Decimal(str(row["temperature_mean_c"])) == Decimal("26.1")
            assert Decimal(str(row["precipitation_mm"])) == Decimal("3.4")
            assert row["holiday_name"] == "New Year's Day"
            assert row["is_public_holiday"] is True
            missing_status, _missing_headers, missing = _read(
                server,
                "/api/v1/factors/daily?location_id=ph-manila&start_date=2026-10-20&end_date=2026-10-20",
            )
            assert missing_status == 200
            assert missing["data"][0]["temperature_mean_c"] is None
            assert missing["data"][0]["holiday_name"] is None
            catalog_status, _catalog_headers, catalog = _read(server, "/api/v1/datasets")
            assert catalog_status == 200
            assert catalog["datasets"][0]["active_release_id"] == body["release_id"]
        finally:
            server.shutdown()
            server.server_close()


def _read(server, path: str) -> tuple[int, dict[str, str], dict]:
    port = server.server_address[1]
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
        return error.code, dict(error.headers), payload
