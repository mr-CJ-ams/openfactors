"""OF-018 Asia/Manila dates and the approved location keys."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from contracts.calendar import date_parts, manila_date, manila_today
from ops.dimensions import approved_locations, load_locations, sync
from ops.local_stack import dev_database_ready
from ops.migrate import apply, psql

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "contracts" / "fixtures" / "manila_date_cases.json"
APPROVED_LOCATION_IDS = (
    "ph-baguio",
    "ph-cebu-city",
    "ph-davao-city",
    "ph-manila",
    "ph-tacloban",
)


def test_manila_date_boundaries_and_weekends() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert cases["timezone"] == "Asia/Manila"
    assert cases["utc_offset"] == "+08:00"
    seen = set()
    for case in cases["cases"]:
        seen.add(case["name"])
        local_date = manila_date(case["instant"])
        assert local_date.isoformat() == case["date"]
        parts = date_parts(local_date)
        assert parts["day_of_week"] == case["day_of_week"]
        assert parts["is_weekend"] is case["is_weekend"]
        assert parts["calendar_year"] == local_date.year
        assert parts["calendar_month"] == local_date.month
    assert "manila_midnight" in seen
    assert "leap_day" in seen
    with pytest.raises(ValueError, match="timezone-aware"):
        manila_date("2018-01-01T00:00:00")


def test_approved_location_keys_match_the_pilot_fixture() -> None:
    fixture = load_locations()
    rows = approved_locations(fixture)
    assert tuple(sorted(row["location_id"] for row in rows)) == APPROVED_LOCATION_IDS
    start = fixture["supported_start_date"]
    for row in rows:
        assert row["country_code"] == "PH"
        assert row["timezone"] == "Asia/Manila"
        assert row["valid_from"] == start
        assert row["valid_to"] is None
        assert row["approved_for_mvp"] is True
        assert row["provider_location_ref"]
        assert "ERA5" in row["weather_grid_description"]


def test_dev_database_holds_locations_and_the_manila_spine() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to load the core dimensions.")
    apply("dev")
    sync("dev")
    end = manila_today()
    fixture = load_locations()
    start = date.fromisoformat(fixture["supported_start_date"])
    stored_locations = _rows(
        """
        SELECT location_id, display_name, btrim(country_code),
               COALESCE(admin_region, ''), COALESCE(admin_province, ''),
               latitude::text, longitude::text, timezone,
               provider_location_ref, weather_grid_description,
               valid_from::text,
               CASE WHEN valid_to IS NULL THEN 't' ELSE 'f' END,
               CASE WHEN approved_for_mvp THEN 't' ELSE 'f' END
        FROM core.dim_location
        ORDER BY location_id;
        """
    )
    expected_rows = sorted(approved_locations(fixture), key=lambda row: row["location_id"])
    assert len(stored_locations) == len(expected_rows)
    for stored, location in zip(stored_locations, expected_rows, strict=True):
        fields = stored.split("|")
        assert fields[0] == location["location_id"]
        assert fields[1] == location["display_name"]
        assert fields[2] == location["country_code"]
        assert fields[3] == (location["admin_region"] or "")
        assert fields[4] == (location["admin_province"] or "")
        assert Decimal(fields[5]) == location["latitude"]
        assert Decimal(fields[6]) == location["longitude"]
        assert fields[7:] == [
            location["timezone"],
            location["provider_location_ref"],
            location["weather_grid_description"],
            location["valid_from"],
            "t",
            "t",
        ]
    bounds = _rows(
        """
        SELECT COUNT(*)::text, MIN(date)::text, MAX(date)::text
        FROM core.dim_date;
        """
    )
    assert bounds == [f"{(end - start).days + 1}|{start.isoformat()}|{end.isoformat()}"]
    assert _rows("SELECT COUNT(*)::text FROM core.dim_date WHERE date < DATE '2018-01-01';") == [
        "0"
    ]
    wanted = ", ".join(f"DATE '{case['date']}'" for case in _cases())
    stored_dates = {
        line.split("|", 1)[0]: line
        for line in _rows(
            f"""
            SELECT date::text, day_of_week::text, CASE WHEN is_weekend THEN 't' ELSE 'f' END,
                   calendar_year::text, calendar_month::text
            FROM core.dim_date
            WHERE date IN ({wanted})
            ORDER BY date;
            """
        )
    }
    for case in _cases():
        parts = date_parts(date.fromisoformat(case["date"]))
        weekend = "t" if parts["is_weekend"] else "f"
        assert stored_dates[case["date"]] == (
            f"{case['date']}|{parts['day_of_week']}|{weekend}|"
            f"{parts['calendar_year']}|{parts['calendar_month']}"
        )
    before = bounds
    sync("dev")
    after = _rows("SELECT COUNT(*)::text, MIN(date)::text, MAX(date)::text FROM core.dim_date;")
    assert after == before
    location_count = _rows("SELECT COUNT(*)::text FROM core.dim_location;")
    assert location_count == [str(len(APPROVED_LOCATION_IDS))]


def _cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _rows(sql: str) -> list[str]:
    completed = psql("dev", "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]
