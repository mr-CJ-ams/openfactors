"""OF-020 public gold columns and the data dictionary."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from contracts.check_dataset_contract import field_names, load_json
from ops.local_stack import docker_available, load_secrets
from ops.migrate import apply, psql

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM = ROOT / "transform"
CONTRACT_PATH = ROOT / "contracts" / "fixtures" / "daily_factor_contract.json"
DICTIONARY_PATH = ROOT / "contracts" / "fixtures" / "data_dictionary.json"
SOURCES_PATH = ROOT / "contracts" / "fixtures" / "location_sources.json"
LOCATIONS_PATH = ROOT / "contracts" / "fixtures" / "pilot_locations.json"
SEED_PATH = TRANSFORM / "seeds" / "location_source_map.csv"
OPENAPI_PATH = ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"
COLUMN_KEYS = (
    "name",
    "logical_type",
    "physical_type",
    "nullable",
    "unit",
    "description",
    "source",
    "transformation",
    "examples",
)
DATASET_KEYS = (
    "dataset_id",
    "dataset_version",
    "release_id",
    "grain",
    "business_key",
    "timezone",
    "geographic_coverage",
    "temporal_coverage",
    "data_as_of",
    "last_successful_publication",
    "next_scheduled_attempt",
    "known_gaps",
    "revision_policy",
    "license",
    "attribution",
    "source_links",
    "provider_spatial_resolution",
    "owner",
    "quality_report_summary",
    "deprecation_policy",
)
PHYSICAL_TYPES = {
    "DATE": ("date", "", "", ""),
    "TEXT": ("text", "", "", ""),
    "CHAR(2)": ("character", "2", "", ""),
    "NUMERIC(7,3)": ("numeric", "", "7", "3"),
    "NUMERIC(10,3)": ("numeric", "", "10", "3"),
    "BOOLEAN": ("boolean", "", "", ""),
    "TIMESTAMPTZ": ("timestamp with time zone", "", "", ""),
    "UUID": ("uuid", "", "", ""),
}


def test_dictionary_matches_the_contract_and_openapi() -> None:
    contract = load_json(CONTRACT_PATH)
    dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    names = field_names(contract)
    columns = dictionary["columns"]
    assert [column["name"] for column in columns] == names
    for column, field in zip(columns, contract["fields"], strict=True):
        assert set(COLUMN_KEYS) <= set(column)
        assert column["physical_type"] == field["postgres"]
        assert column["nullable"] is field["nullable"]
        assert column["logical_type"] == field["json_type"]
        assert column["description"]
        assert isinstance(column["examples"], list)
    assert [column["name"] for column in dictionary["internal_columns"]] == [
        "weather_data_status",
        "release_id",
    ]
    assert all(set(COLUMN_KEYS) <= set(column) for column in dictionary["internal_columns"])
    assert dictionary["dataset_id"] == contract["dataset_id"]
    assert dictionary["dataset_version"] == contract["dataset_version"]
    assert dictionary["timezone"] == contract["timezone"]
    assert dictionary["grain"] == contract["grain"]
    for key in DATASET_KEYS:
        assert key in dictionary
    assert dictionary["release_id"] is None
    assert dictionary["data_as_of"] is None
    assert dictionary["last_successful_publication"] is None
    assert dictionary["quality_report_summary"] is None
    assert "25 km" in dictionary["provider_spatial_resolution"]
    assert "Open-Meteo.com" in dictionary["attribution"]
    assert "Official Gazette" in dictionary["attribution"]
    project = yaml.safe_load((TRANSFORM / "dbt_project.yml").read_text(encoding="utf-8"))
    assert project["vars"]["dataset_version"] == contract["dataset_version"]
    openapi = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    factor = openapi["components"]["schemas"]["DailyFactor"]
    assert factor["required"] == names
    assert list(factor["properties"]) == names
    assert "weather_data_status" not in factor["properties"]
    assert "release_id" not in factor["properties"]


def test_location_source_map_matches_the_pilot_cities() -> None:
    pinned = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["locations"]
    locations = json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"))["locations"]
    assert [row["location_id"] for row in pinned] == [row["location_id"] for row in locations]
    with SEED_PATH.open(encoding="utf-8", newline="") as handle:
        seeded = list(csv.DictReader(handle))
    assert seeded == [
        {
            "location_id": row["location_id"],
            "weather_source_id": row["weather_source_id"],
            "holiday_source_id": row["holiday_source_id"],
        }
        for row in pinned
    ]
    assert {row["weather_source_id"] for row in pinned} == {"open-meteo-era5"}
    assert {row["holiday_source_id"] for row in pinned} == {"ph-official-gazette"}


def test_candidate_schema_snapshot() -> None:
    if not docker_available():
        pytest.skip("Docker is required to read the gold mart schema.")
    if not Path(sys.executable).with_name("dbt.exe").is_file():
        pytest.skip("dbt is not installed in the project environment.")
    apply("dev")
    built = _run_dbt(_dbt_env())
    assert built.returncode == 0, built.stdout + built.stderr
    contract = load_json(CONTRACT_PATH)
    dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    expected = [(field["name"], *PHYSICAL_TYPES[field["postgres"]]) for field in contract["fields"]]
    expected.extend(
        (column["name"], *PHYSICAL_TYPES[column["physical_type"]])
        for column in dictionary["internal_columns"]
    )
    snapshot = _rows(
        """
        SELECT column_name, data_type,
               COALESCE(character_maximum_length::text, ''),
               COALESCE(numeric_precision::text, ''),
               COALESCE(numeric_scale::text, '')
        FROM information_schema.columns
        WHERE table_schema = 'staging'
          AND table_name = 'daily_external_factors_ph_candidate'
        ORDER BY ordinal_position;
        """
    )
    assert [tuple(line.split("|")) for line in snapshot] == [tuple(row) for row in expected]


def _dbt_env() -> dict[str, str]:
    secrets = load_secrets("dev")
    env = os.environ.copy()
    env["OPENFACTORS_PGHOST"] = "127.0.0.1"
    env["OPENFACTORS_PGPORT"] = secrets["POSTGRES_PORT"]
    env["OPENFACTORS_PGDATABASE"] = secrets["POSTGRES_DB"]
    env["OPENFACTORS_PGPASSWORD"] = secrets["OF_TRANSFORM_PASSWORD"]
    return env


def _run_dbt(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("dbt.exe")
    completed = subprocess.run(
        [
            str(executable),
            "build",
            "--project-dir",
            str(TRANSFORM),
            "--profiles-dir",
            str(TRANSFORM),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    secret = env["OPENFACTORS_PGPASSWORD"]
    completed.stdout = completed.stdout.replace(secret, "***")
    completed.stderr = completed.stderr.replace(secret, "***")
    return completed


def _rows(sql: str) -> list[str]:
    completed = psql("dev", "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]
