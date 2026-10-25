"""Join sample outcomes to one pinned factor release.

The sample orders are not a measured business result. The join lines the
outcome up with the factor row. It does not show that weather or a holiday
caused the orders.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

SAMPLE_RELEASE_ID = "00000000-0000-4000-8000-000000000045"
SAMPLE_FACTORS = (
    {
        "date": "2018-01-01",
        "location_id": "ph-manila",
        "temperature_mean_c": "26.1",
        "precipitation_mm": "3.4",
        "is_public_holiday": False,
    },
)
SAMPLE_BUSINESS = (
    {"location_id": "ph-manila", "date": "2018-01-01", "orders": 4},
    {"location_id": "ph-manila", "date": "2018-01-02", "orders": 5},
)
_SQL_PATH = Path(__file__).with_name("join_factors.sql")
_PIN = "__RELEASE_ID__"


def join_outcomes(
    release_id: str,
    factors: list[dict] | tuple[dict, ...],
    business: list[dict] | tuple[dict, ...],
) -> list[dict]:
    """Return the inner join for one release. A missing factor stays missing."""
    pinned = _pin(release_id)
    index: dict[tuple[str, str], dict] = {}
    for row in factors:
        if "release_id" in row and str(row["release_id"]) != pinned:
            raise ValueError("A factor row belongs to another release.")
        key = (str(row["location_id"]), str(row["date"]))
        if key in index:
            raise ValueError("A factor row is duplicated.")
        index[key] = row
    joined = []
    for outcome in business:
        if isinstance(outcome.get("orders"), bool) or not isinstance(outcome.get("orders"), int):
            raise ValueError("orders must be a whole number.")
        key = (str(outcome["location_id"]), str(outcome["date"]))
        factor = index.get(key)
        if factor is None:
            continue
        joined.append(
            {
                "release_id": pinned,
                "location_id": key[0],
                "date": key[1],
                "orders": outcome["orders"],
                "temperature_mean_c": factor.get("temperature_mean_c"),
                "precipitation_mm": factor.get("precipitation_mm"),
                "is_public_holiday": factor.get("is_public_holiday"),
            }
        )
    return joined


def render_sql(release_id: str) -> str:
    """Fill the one release pin. The sample orders stay in the SQL file."""
    pinned = _pin(release_id)
    statement = _SQL_PATH.read_text(encoding="utf-8")
    if statement.count(_PIN) != 1:
        raise ValueError("The example SQL must pin one release.")
    return statement.replace(_PIN, pinned)


def run_join(environment: str, release_id: str) -> list[dict]:
    """Run the example SQL as the public role. The pin is one UUID."""
    from ops.migrate import psql

    statement = render_sql(release_id).rstrip().rstrip(";")
    wrapped = (
        "SELECT COALESCE(json_agg(row_to_json(joined)), '[]'::json)::text "
        f"FROM ({statement}) AS joined;"
    )
    completed = psql(environment, "of_api", wrapped, tuples_only=True)
    if completed.returncode != 0:
        raise RuntimeError("The example SQL did not run.")
    payload = _json_line(completed.stdout)
    rows = []
    for item in payload:
        flag = item["is_public_holiday"]
        rows.append(
            {
                "location_id": item["location_id"],
                "date": item["date"],
                "orders": item["orders"],
                "temperature_mean_c": item["temperature_mean_c"],
                "precipitation_mm": item["precipitation_mm"],
                "is_public_holiday": flag == "true",
            }
        )
    return rows


def _pin(release_id: str) -> str:
    try:
        return str(uuid.UUID(release_id))
    except ValueError:
        raise ValueError("release_id must be a UUID.") from None


def _json_line(text: str) -> list:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            payload = json.loads(stripped)
            if isinstance(payload, list):
                return payload
    raise RuntimeError("The example SQL did not run.")
