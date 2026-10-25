"""Map the source-registry fixture onto control.source_registry columns."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIXTURE = ROOT / "contracts" / "fixtures" / "source_registry.json"
RETENTION_FIXTURE = ROOT / "contracts" / "fixtures" / "retention_policy.json"

GRAINS = {
    "weather": "location_id, Asia/Manila date",
    "holiday": "national Asia/Manila date",
}
COMMERCIAL_USE_ALLOWED = {
    "forbidden_on_free_api": False,
    "mvp_is_noncommercial": False,
}
APPROVED_BY = "Founder / Product Lead"

COLUMNS = (
    "source_id",
    "provider_name",
    "source_url",
    "source_grain",
    "license_reference_url",
    "redistribution_status",
    "attribution_text",
    "commercial_use_allowed",
    "retention_mode",
    "raw_retention_days",
    "public_export_allowed",
    "approved_by",
    "approved_at",
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def source_rows(registry: dict, retention: dict) -> list[dict]:
    raw_files = retention["raw_files"]
    mode = raw_files["mode"]
    days = raw_files["automatic_expiry_days"]
    if mode != "until_source_withdrawn" or days is not None:
        raise ValueError("The seed only accepts until_source_withdrawn with no day count.")
    adopted_on = registry["adopted_on"]
    rows: list[dict] = []
    for source in registry["sources"]:
        role = source["role"]
        commercial_use = source["commercial_use"]
        license_status = source["license_status"]
        if role not in GRAINS:
            raise ValueError(f"No source grain is recorded for role {role!r}.")
        if commercial_use not in COMMERCIAL_USE_ALLOWED:
            raise ValueError(f"No commercial-use decision is recorded for {commercial_use!r}.")
        if not str(license_status).startswith("approved"):
            raise ValueError(f"Source {source['source_id']} is not an approved baseline.")
        terms_url = source["terms_url"]
        endpoint = source.get("endpoint")
        rows.append(
            {
                "source_id": source["source_id"],
                "provider_name": source["provider"],
                "source_url": endpoint or terms_url,
                "source_grain": GRAINS[role],
                "license_reference_url": source.get("license_url") or terms_url,
                "redistribution_status": "approved",
                "attribution_text": source["attribution"],
                "commercial_use_allowed": COMMERCIAL_USE_ALLOWED[commercial_use],
                "retention_mode": mode,
                "raw_retention_days": None,
                "public_export_allowed": bool(
                    source["public_api_allowed"] and source["csv_parquet_allowed"]
                ),
                "approved_by": APPROVED_BY,
                "approved_at": f"{adopted_on}T00:00:00+08:00",
            }
        )
    return sorted(rows, key=lambda row: str(row["source_id"]))


def seed_sql(registry: dict, retention: dict) -> str:
    rows = source_rows(registry, retention)
    column_list = ",\n  ".join(COLUMNS)
    values = ",\n".join(_values_tuple(row) for row in rows)
    return f"INSERT INTO control.source_registry (\n  {column_list}\n) VALUES\n{values};\n"


def _values_tuple(row: dict) -> str:
    rendered = ",\n  ".join(_sql_value(row[column]) for column in COLUMNS)
    return f"(\n  {rendered}\n)"


def _sql_value(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if not isinstance(value, str):
        raise TypeError(f"Cannot render {value!r} as SQL.")
    return "'" + value.replace("'", "''") + "'"
