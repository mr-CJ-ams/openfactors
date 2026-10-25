"""Publish-blocking checks for schema, keys, locations, dates, and sources.

A FAIL result is not written to the publish table.
"""

from __future__ import annotations

from dataclasses import dataclass

INTERNAL_COLUMNS = (
    {"name": "weather_data_status", "physical_type": "TEXT", "nullable": False},
    {"name": "release_id", "physical_type": "UUID", "nullable": True},
)
_SNAPSHOT_TYPES = {
    ("date", "", "", ""): "DATE",
    ("text", "", "", ""): "TEXT",
    ("character", "2", "", ""): "CHAR(2)",
    ("numeric", "", "7", "3"): "NUMERIC(7,3)",
    ("numeric", "", "10", "3"): "NUMERIC(10,3)",
    ("boolean", "", "", ""): "BOOLEAN",
    ("timestamp with time zone", "", "", ""): "TIMESTAMPTZ",
    ("uuid", "", "", ""): "UUID",
}


@dataclass(frozen=True)
class GateResult:
    status: str
    violations: tuple[str, ...]


def physical_type(data_type: str, char_length: str, precision: str, scale: str) -> str:
    return _SNAPSHOT_TYPES.get((data_type, char_length, precision, scale), "UNKNOWN")


def evaluate(
    rows: list[dict],
    columns: list[dict],
    *,
    contract_fields: list[dict],
    locations: set[str],
    dates: set[str],
    sources: set[str],
) -> GateResult:
    violations: set[str] = set()
    if _schema_failed(columns, contract_fields):
        violations.add("schema")
    else:
        _row_violations(rows, contract_fields, locations, dates, sources, violations)
    if not violations:
        return GateResult("PASS", ())
    order = (
        "schema",
        "null_required",
        "duplicate_location_date",
        "unknown_location",
        "unknown_date",
        "unapproved_source",
    )
    return GateResult("FAIL", tuple(code for code in order if code in violations))


def _schema_failed(columns: list[dict], contract_fields: list[dict]) -> bool:
    expected = [
        {
            "name": field["name"],
            "physical_type": field["postgres"],
            "nullable": field["nullable"],
        }
        for field in contract_fields
    ]
    expected.extend(INTERNAL_COLUMNS)
    if len(columns) != len(expected):
        return True
    for column, spec in zip(columns, expected, strict=True):
        if column["name"] != spec["name"]:
            return True
        if column["physical_type"] != spec["physical_type"]:
            return True
        if column["nullable"] is not spec["nullable"]:
            return True
    return False


def _row_violations(
    rows: list[dict],
    contract_fields: list[dict],
    locations: set[str],
    dates: set[str],
    sources: set[str],
    violations: set[str],
) -> None:
    seen: set[tuple[object, object]] = set()
    required = {field["name"] for field in contract_fields if not field["nullable"]}
    for row in rows:
        for name in required:
            if row.get(name) is None:
                violations.add("null_required")
        location_id = row.get("location_id")
        local_date = row.get("date")
        if location_id is not None and location_id not in locations:
            violations.add("unknown_location")
        if local_date is not None and local_date not in dates:
            violations.add("unknown_date")
        for source_name in ("weather_source_id", "holiday_source_id"):
            source_id = row.get(source_name)
            if source_id is not None and source_id not in sources:
                violations.add("unapproved_source")
        key = (location_id, local_date)
        if location_id is not None and local_date is not None:
            if key in seen:
                violations.add("duplicate_location_date")
            seen.add(key)
