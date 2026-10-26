"""OF-021 schema, uniqueness, and reference gates block a bad candidate."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.local_stack import dbt_executable, dev_database_ready, load_secrets
from ops.migrate import apply, psql

from openfactors.quality.gates import INTERNAL_COLUMNS, evaluate, physical_type

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM = ROOT / "transform"
CASES_PATH = ROOT / "contracts" / "fixtures" / "gate_cases.json"
LOCATIONS_PATH = ROOT / "contracts" / "fixtures" / "pilot_locations.json"


def _columns(contract: dict) -> list[dict]:
    columns = [
        {
            "name": field["name"],
            "physical_type": field["postgres"],
            "nullable": field["nullable"],
        }
        for field in contract["fields"]
    ]
    columns.extend(dict(column) for column in INTERNAL_COLUMNS)
    return columns


def _context(base: dict) -> tuple[set[str], set[str], set[str]]:
    locations = json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"))["locations"]
    approved = {row["location_id"] for row in locations if row["approved_for_mvp"] is True}
    return approved, {base["date"]}, {"open-meteo-era5", "ph-official-gazette"}


def test_bad_fixtures_block_and_the_known_good_row_passes() -> None:
    document = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    contract = load_json(CONTRACT_PATH)
    base = document["base_row"]
    locations, dates, sources = _context(base)
    assert base["location_id"] in locations
    for case in document["cases"]:
        columns = _columns(contract)
        row = dict(base)
        row.update(case.get("set", {}))
        rows = [row, dict(row)] if case.get("duplicate") else [row]
        if "drop_column" in case:
            columns = [column for column in columns if column["name"] != case["drop_column"]]
        if "swap" in case:
            left_name, right_name = case["swap"]
            index = {column["name"]: position for position, column in enumerate(columns)}
            left = index[left_name]
            right = index[right_name]
            columns[left], columns[right] = columns[right], columns[left]
        for name, physical in case.get("retype", {}).items():
            for column in columns:
                if column["name"] == name:
                    column["physical_type"] = physical
        for name, nullable in case.get("nullable", {}).items():
            for column in columns:
                if column["name"] == name:
                    column["nullable"] = nullable
        result = evaluate(
            rows,
            columns,
            contract_fields=contract["fields"],
            locations=locations,
            dates=dates,
            sources=sources,
        )
        if case["expect"] == "PASS":
            assert result.status == "PASS", case["name"]
            assert result.violations == ()
        else:
            assert result.status == "FAIL", case["name"]
            assert list(result.violations) == case["expect"], case["name"]


def test_live_candidate_passes_and_published_rows_stay_put() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to gate the candidate mart.")
    if not dbt_executable().is_file():
        pytest.skip("dbt is not installed in the project environment.")
    apply("dev")
    built = _run_dbt(_dbt_env())
    assert built.returncode == 0, built.stdout + built.stderr
    before = _rows("SELECT count(*)::text FROM publish.daily_external_factors_ph;")
    contract = load_json(CONTRACT_PATH)
    columns = _live_columns(contract)
    payload = json.loads(_rows(_candidate_sql())[0])
    result = evaluate(
        payload,
        columns,
        contract_fields=contract["fields"],
        locations=set(_rows("SELECT location_id FROM core.dim_location WHERE approved_for_mvp;")),
        dates=set(_rows("SELECT date::text FROM core.dim_date;")),
        sources=set(
            _rows(
                "SELECT source_id FROM control.source_registry "
                "WHERE redistribution_status = 'approved';"
            )
        ),
    )
    after = _rows("SELECT count(*)::text FROM publish.daily_external_factors_ph;")
    assert result.status == "PASS"
    assert result.violations == ()
    assert before == after


def _live_columns(contract: dict) -> list[dict]:
    nullable = {field["name"]: field["nullable"] for field in contract["fields"]}
    nullable.update({column["name"]: column["nullable"] for column in INTERNAL_COLUMNS})
    columns = []
    for line in _rows(
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
    ):
        name, data_type, char_length, precision, scale = line.split("|")
        columns.append(
            {
                "name": name,
                "physical_type": physical_type(data_type, char_length, precision, scale),
                "nullable": nullable.get(name),
            }
        )
    return columns


def _candidate_sql() -> str:
    return """
    SELECT COALESCE(json_agg(row_to_json(candidate)), '[]'::json)::text
    FROM (
      SELECT date::text AS date,
             location_id,
             btrim(country_code) AS country_code,
             temperature_mean_c,
             precipitation_mm,
             is_public_holiday,
             holiday_name,
             is_weekend,
             weather_source_id,
             holiday_source_id,
             source_updated_at,
             dataset_version
      FROM staging.daily_external_factors_ph_candidate
    ) AS candidate;
    """


def _dbt_env() -> dict[str, str]:
    secrets = load_secrets("dev")
    env = os.environ.copy()
    env["OPENFACTORS_PGHOST"] = "127.0.0.1"
    env["OPENFACTORS_PGPORT"] = secrets["POSTGRES_PORT"]
    env["OPENFACTORS_PGDATABASE"] = secrets["POSTGRES_DB"]
    env["OPENFACTORS_PGPASSWORD"] = secrets["OF_TRANSFORM_PASSWORD"]
    return env


def _run_dbt(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    executable = dbt_executable()
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
