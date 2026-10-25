"""ERA5 local-day mapping, including one live archive check."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from openfactors.ingestion.checkpoints import CheckpointKey, MemoryCheckpoints
from openfactors.ingestion.era5 import (
    SOURCE_ID,
    build_archive_request,
    fetch_era5_daily,
    parse_era5_daily,
    urllib_transport,
)
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.http_client import HttpConnector, HttpResponse

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "contracts" / "fixtures" / "open_meteo_era5_2018-01-01.json"
LOCATIONS = ROOT / "contracts" / "fixtures" / "pilot_locations.json"
HISTORY = ROOT / "ingestion" / "raw" / "open-meteo-era5" / "manifest.json"
DAY = date(2018, 1, 1)
HISTORY_END = date(2026, 10, 25)
NULL_DATES = tuple(date(2026, 10, day) for day in range(20, 26))


def _payload(*, timezone: str = "Asia/Manila", first_precip: str = "0.0") -> bytes:
    return f"""{{
        "latitude": 14.5,
        "longitude": 121.0,
        "elevation": 4.0,
        "timezone": "{timezone}",
        "utc_offset_seconds": 28800,
        "daily_units": {{
            "time": "iso8601",
            "temperature_2m_mean": "°C",
            "precipitation_sum": "mm"
        }},
        "daily": {{
            "time": ["2018-01-02", "2018-01-03"],
            "temperature_2m_mean": [26.0, null],
            "precipitation_sum": [{first_precip}, null]
        }}
    }}""".encode()


def _locations() -> list[dict]:
    payload = json.loads(LOCATIONS.read_text(encoding="utf-8"), parse_float=Decimal)
    return payload["locations"]


def _archive_body(row: dict) -> bytes:
    return f"""{{
        "latitude": {row["grid_latitude"]},
        "longitude": {row["grid_longitude"]},
        "elevation": {row["elevation_m"]},
        "timezone": "Asia/Manila",
        "utc_offset_seconds": 28800,
        "daily_units": {{
            "time": "iso8601",
            "temperature_2m_mean": "°C",
            "precipitation_sum": "mm"
        }},
        "daily": {{
            "time": ["{row["date"]}"],
            "temperature_2m_mean": [{row["temperature_2m_mean_c"]}],
            "precipitation_sum": [{row["precipitation_sum_mm"]}]
        }}
    }}""".encode()


def test_archive_request_uses_era5_and_manila_timezone() -> None:
    request = build_archive_request(Decimal("14.5906"), Decimal("120.9799"), DAY, DAY)
    assert request.method == "GET"
    assert request.url.startswith("https://archive-api.open-meteo.com/v1/archive?")
    assert "models=era5" in request.url
    assert "timezone=Asia%2FManila" in request.url
    assert "daily=temperature_2m_mean%2Cprecipitation_sum" in request.url
    assert "latitude=14.5906" in request.url
    assert "longitude=120.9799" in request.url


def test_missing_stays_null_and_zero_stays_zero() -> None:
    parsed = parse_era5_daily("ph-manila", _payload())
    assert parsed.provider_location_ref == "14.50,121.00"
    assert "0.25 degree" in parsed.weather_grid_description
    assert parsed.rows[0].precipitation_mm == Decimal("0.0")
    assert parsed.rows[0].temperature_mean_c == Decimal("26.0")
    assert parsed.rows[1].temperature_mean_c is None
    assert parsed.rows[1].precipitation_mm is None
    assert parsed.rows[0].source_updated_at is None
    assert parsed.rows[0].date == date(2018, 1, 2)


def test_wrong_timezone_is_schema_failure() -> None:
    with pytest.raises(ConnectorError) as caught:
        parse_era5_daily("ph-manila", _payload(timezone="UTC"))
    assert caught.value.category is ErrorCategory.SCHEMA


def test_negative_precipitation_is_schema_failure() -> None:
    with pytest.raises(ConnectorError) as caught:
        parse_era5_daily("ph-manila", _payload(first_precip="-1"))
    assert caught.value.category is ErrorCategory.SCHEMA


def test_recorded_probe_matches_the_location_mapping() -> None:
    probe = json.loads(PROBE.read_text(encoding="utf-8"), parse_float=Decimal)
    locations = {row["location_id"]: row for row in _locations()}
    for row in probe["rows"]:
        parsed = parse_era5_daily(row["location_id"], _archive_body(row))
        location = locations[row["location_id"]]
        assert parsed.rows[0].date == DAY
        assert parsed.rows[0].temperature_mean_c == row["temperature_2m_mean_c"]
        assert parsed.rows[0].precipitation_mm == row["precipitation_sum_mm"]
        assert parsed.rows[0].source_updated_at is None
        assert parsed.provider_location_ref == location["provider_location_ref"]
        assert parsed.weather_grid_description == location["weather_grid_description"]


def test_fetch_does_not_move_checkpoint() -> None:
    def transport(request, timeout):
        return HttpResponse(200, _payload())

    checkpoints = MemoryCheckpoints()
    key = CheckpointKey(SOURCE_ID, "ph-manila", "daily")
    parsed = fetch_era5_daily(
        HttpConnector(transport),
        location_id="ph-manila",
        latitude=Decimal("14.5906"),
        longitude=Decimal("120.9799"),
        start=date(2018, 1, 2),
        end=date(2018, 1, 3),
    )
    assert len(parsed.rows) == 2
    assert checkpoints.get(key) is None


@pytest.mark.skipif(
    os.environ.get("OPENFACTORS_LIVE_WEATHER") != "1",
    reason="Set OPENFACTORS_LIVE_WEATHER=1 to call the Open-Meteo archive.",
)
def test_live_2018_01_01_matches_recorded_probe() -> None:
    probe = json.loads(PROBE.read_text(encoding="utf-8"), parse_float=Decimal)
    by_id = {row["location_id"]: row for row in probe["rows"]}
    connector = HttpConnector(urllib_transport)
    for location in _locations():
        parsed = fetch_era5_daily(
            connector,
            location_id=location["location_id"],
            latitude=Decimal(str(location["latitude"])),
            longitude=Decimal(str(location["longitude"])),
            start=DAY,
            end=DAY,
        )
        expected = by_id[location["location_id"]]
        assert parsed.rows[0].date == DAY
        assert parsed.rows[0].temperature_mean_c == expected["temperature_2m_mean_c"]
        assert parsed.rows[0].precipitation_mm == expected["precipitation_sum_mm"]
        assert parsed.grid_latitude == expected["grid_latitude"]
        assert parsed.grid_longitude == expected["grid_longitude"]
        assert parsed.elevation_m == expected["elevation_m"]
        assert parsed.provider_location_ref == location["provider_location_ref"]


def test_stored_history_keeps_the_probe_cell_and_the_null_tail() -> None:
    manifest = json.loads(HISTORY.read_text(encoding="utf-8"))
    locations = {row["location_id"]: row for row in _locations()}
    probe_rows = json.loads(PROBE.read_text(encoding="utf-8"))["rows"]
    probe = {row["location_id"]: row for row in probe_rows}
    assert manifest["start_date"] == DAY.isoformat()
    assert manifest["end_date"] == HISTORY_END.isoformat()
    assert [row["location_id"] for row in manifest["locations"]] == list(locations)
    expected_days = (HISTORY_END - DAY).days + 1
    for record in manifest["locations"]:
        body = (ROOT / record["object"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == record["sha256"]
        parsed = parse_era5_daily(record["location_id"], body)
        location = locations[record["location_id"]]
        assert parsed.provider_location_ref == location["provider_location_ref"]
        assert parsed.rows[0].date == DAY
        assert parsed.rows[-1].date == HISTORY_END
        assert len(parsed.rows) == expected_days
        expected = probe[record["location_id"]]
        assert parsed.rows[0].temperature_mean_c == Decimal(str(expected["temperature_2m_mean_c"]))
        assert parsed.rows[0].precipitation_mm == Decimal(str(expected["precipitation_sum_mm"]))
        nulls = [
            row.date
            for row in parsed.rows
            if row.temperature_mean_c is None or row.precipitation_mm is None
        ]
        assert tuple(nulls) == NULL_DATES
        assert record["null_dates"] == [day.isoformat() for day in NULL_DATES]
