"""Load the approved pilot locations and the Asia/Manila date spine.

The spine ends on the current Manila date. That end is not a product maximum.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from contracts.calendar import manila_today

ROOT = Path(__file__).resolve().parents[1]
LOCATIONS_PATH = ROOT / "contracts" / "fixtures" / "pilot_locations.json"

LOCATION_COLUMNS = (
    "location_id",
    "display_name",
    "country_code",
    "admin_region",
    "admin_province",
    "latitude",
    "longitude",
    "timezone",
    "provider_location_ref",
    "weather_grid_description",
    "valid_from",
    "valid_to",
    "approved_for_mvp",
)


def load_locations() -> dict:
    return json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"), parse_float=Decimal)


def approved_locations(fixture: dict | None = None) -> list[dict]:
    document = load_locations() if fixture is None else fixture
    if document.get("timezone") != "Asia/Manila":
        raise ValueError("Pilot locations must use Asia/Manila.")
    start = date.fromisoformat(document["supported_start_date"])
    rows: list[dict] = []
    seen: set[str] = set()
    for location in document["locations"]:
        location_id = location["location_id"]
        if location_id in seen:
            raise ValueError(f"Duplicate location_id {location_id}.")
        seen.add(location_id)
        if location["approved_for_mvp"] is not True:
            raise ValueError(f"{location_id} is not approved for the MVP.")
        if location["country_code"] != "PH" or location["timezone"] != "Asia/Manila":
            raise ValueError(f"{location_id} must be a Philippine Asia/Manila location.")
        if date.fromisoformat(location["valid_from"]) != start or location["valid_to"] is not None:
            raise ValueError(f"{location_id} must be open from the supported start date.")
        if not location["provider_location_ref"] or not location["weather_grid_description"]:
            raise ValueError(f"{location_id} is missing its ERA5 cell.")
        rows.append(location)
    if not rows:
        raise ValueError("The location fixture has no approved rows.")
    return rows


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_value(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return f"DATE {_sql_literal(value.isoformat())}"
    return _sql_literal(str(value))


def sync_sql(*, today: date | None = None, fixture: dict | None = None) -> str:
    document = load_locations() if fixture is None else fixture
    start = date.fromisoformat(document["supported_start_date"])
    end = manila_today() if today is None else today
    if end < start:
        raise ValueError("The Manila date spine cannot end before the supported start.")
    rows = approved_locations(document)
    values = ",\n  ".join(
        "(" + ", ".join(_sql_value(location[column]) for column in LOCATION_COLUMNS) + ")"
        for location in rows
    )
    columns = ", ".join(LOCATION_COLUMNS)
    return f"""
    INSERT INTO core.dim_location ({columns})
    VALUES
      {values}
    ON CONFLICT (location_id) DO NOTHING;
    INSERT INTO core.dim_date (
      date, day_of_week, is_weekend, calendar_year, calendar_month
    )
    SELECT
      series.day::date,
      EXTRACT(ISODOW FROM series.day)::smallint,
      EXTRACT(ISODOW FROM series.day)::integer IN (6, 7),
      EXTRACT(YEAR FROM series.day)::smallint,
      EXTRACT(MONTH FROM series.day)::smallint
    FROM generate_series(
      DATE {_sql_literal(start.isoformat())},
      DATE {_sql_literal(end.isoformat())},
      INTERVAL '1 day'
    ) AS series(day)
    ON CONFLICT (date) DO NOTHING;
    """


def sync(environment: str, *, today: date | None = None) -> None:
    from ops.migrate import psql

    completed = psql(environment, "of_admin", sync_sql(today=today))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"Dimension load failed.\n{detail}")
