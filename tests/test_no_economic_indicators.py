"""Monthly economic indicators are absent. No daily row is fabricated for them."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from openfactors.api.catalog import catalog_server
from openfactors.api.openapi import implementation_schema

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "fixtures" / "daily_factor_contract.json"
DICTIONARY = ROOT / "contracts" / "fixtures" / "data_dictionary.json"
SOURCES = ROOT / "contracts" / "fixtures" / "source_registry.json"
FIELDS = (
    "date",
    "location_id",
    "country_code",
    "temperature_mean_c",
    "precipitation_mm",
    "is_public_holiday",
    "holiday_name",
    "is_weekend",
    "weather_source_id",
    "holiday_source_id",
    "source_updated_at",
    "dataset_version",
)
ECONOMIC = ("cpi", "gdp", "unemployment", "inflation", "exchange_rate")
PATHS = ("/economics", "/api/v1/economics", "/api/v1/indicators")


def test_daily_contract_has_no_economic_fields_or_route() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    dictionary = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    registry = json.loads(SOURCES.read_text(encoding="utf-8"))
    names = tuple(field["name"] for field in contract["fields"])
    assert names == FIELDS
    assert tuple(column["name"] for column in dictionary["columns"]) == FIELDS
    assert {item["source_id"] for item in registry["sources"]} == {
        "open-meteo-era5",
        "ph-official-gazette",
    }
    lowered = " ".join(names).lower()
    for word in ECONOMIC:
        assert word not in lowered
    assert all("economic" not in path for path in implementation_schema()["paths"])
    server = catalog_server(lambda: [])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in PATHS:
            for method in ("GET", "POST"):
                status, _headers, body = _read(server, path, method)
                assert status == 404
                text = json.dumps(body).lower()
                for word in ECONOMIC:
                    assert word not in text
    finally:
        server.shutdown()
        server.server_close()


def _read(server, path: str, method: str) -> tuple[int, dict[str, str], dict]:
    _host, port = server.server_address
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), json.loads(error.read().decode())
