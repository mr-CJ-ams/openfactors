"""The stored calendar and ERA5 files can be prepared for the local candidate."""

from __future__ import annotations

import json
from pathlib import Path

from ops.load_stored import holiday_landed_payload, weather_records

ROOT = Path(__file__).resolve().parents[1]


def test_holiday_payload_keeps_complete_gazette_years() -> None:
    document = json.loads(
        (ROOT / "contracts" / "fixtures" / "holiday_calendar.json").read_text(encoding="utf-8")
    )
    payload = holiday_landed_payload(document)
    assert payload["declared_status"] == "complete"
    names = {
        item["date"]: item["name"]
        for item in payload["observances"]
        if item["date"] == "2018-01-01"
    }
    assert names["2018-01-01"] == "New Year's Day"
    assert all(item["name"] != "Alpha Day" for item in payload["observances"])
    assert {item["date"][:4] for item in payload["observances"] if item["date"]} >= {
        str(year) for year in range(2018, 2027)
    }


def test_stored_weather_matches_the_history_manifest() -> None:
    manifest = json.loads(
        (ROOT / "ingestion" / "raw" / "open-meteo-era5" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    records = weather_records(manifest)
    assert [item["location_id"] for item in records] == [
        item["location_id"] for item in manifest["locations"]
    ]
    assert all(item["fingerprint"].startswith("stored-open-meteo-era5-") for item in records)
