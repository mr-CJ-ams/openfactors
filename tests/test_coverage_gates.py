"""OF-022 coverage threshold, documented exceptions, and calendar gates."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from contracts.calendar import date_parts
from ops.local_stack import dbt_executable, dev_database_ready, load_secrets
from ops.migrate import apply, psql

from openfactors.quality.coverage import COVERAGE_DENOMINATOR, COVERAGE_NUMERATOR, evaluate_coverage

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM = ROOT / "transform"
CASES_PATH = ROOT / "contracts" / "fixtures" / "coverage_cases.json"


def test_threshold_and_exception_fixtures() -> None:
    document = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert document["not_a_proclamation"] is True
    minimum = Decimal(COVERAGE_NUMERATOR) / Decimal(COVERAGE_DENOMINATOR)
    assert Decimal(document["row_coverage_minimum"]) == minimum
    for case in document["cases"]:
        rows, supported, exclusions, verified = _scenario(document, case)
        result = evaluate_coverage(
            rows,
            supported=supported,
            exclusions=exclusions,
            verified_dates=verified,
        )
        if case["expect"] == "PASS":
            assert result.status == "PASS", case["name"]
            assert result.violations == ()
        else:
            assert result.status == "FAIL", case["name"]
            assert list(result.violations) == case["expect"], case["name"]
        assert result.expected == case["expected_count"], case["name"]
        assert result.present == case["present_count"], case["name"]
        assert result.excluded == case["excluded_count"], case["name"]
        assert result.weather_present == case["weather_present"], case["name"]


def test_current_spine_reports_coverage_and_publish_stays_empty() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to measure the candidate coverage.")
    if not dbt_executable().is_file():
        pytest.skip("dbt is not installed in the project environment.")
    apply("dev")
    built = _run_dbt(_dbt_env())
    assert built.returncode == 0, built.stdout + built.stderr
    before = _rows("SELECT count(*)::text FROM publish.daily_external_factors_ph;")
    locations = _rows("SELECT location_id FROM core.dim_location WHERE approved_for_mvp;")
    days = _rows("SELECT date::text FROM core.dim_date;")
    supported = {(location_id, day) for location_id in locations for day in days}
    verified = {
        item["date"]: {
            "is_public_holiday": item["is_public_holiday"],
            "holiday_name": item["holiday_name"],
        }
        for item in json.loads(_rows(_snapshot_sql())[0])
    }
    result = evaluate_coverage(
        json.loads(_rows(_candidate_sql())[0]),
        supported=supported,
        exclusions=[],
        verified_dates=verified,
    )
    after = _rows("SELECT count(*)::text FROM publish.daily_external_factors_ph;")
    meets = result.expected > 0 and result.present * COVERAGE_DENOMINATOR >= (
        result.expected * COVERAGE_NUMERATOR
    )
    if meets:
        assert "coverage" not in result.violations
    else:
        assert result.status == "FAIL"
        assert "coverage" in result.violations
    assert result.excluded == 0
    assert before == after


def _scenario(
    document: dict,
    case: dict,
) -> tuple[list[dict], set[tuple[str, str]], list[dict], dict[str, dict]]:
    location_id = document["location_id"]
    days = [
        (date(2018, 1, 1) + timedelta(days=offset)).isoformat() for offset in range(case["dates"])
    ]
    present_days = days[: case["present"]]
    omitted = set(days[: case.get("omit_verified", 0)])
    verified = {
        day: {"is_public_holiday": False, "holiday_name": None}
        for day in days
        if day not in omitted
    }
    rows = [_row(document, case, day, index) for index, day in enumerate(present_days)]
    if case.get("unknown_holiday"):
        verified[days[0]] = {"is_public_holiday": True, "holiday_name": None}
    if case.get("holiday_mismatch"):
        verified[days[0]] = {
            "is_public_holiday": True,
            "holiday_name": document["shape_holiday_name"],
        }
    exclusions = _exclusions(document, case, location_id, days)
    supported = {(location_id, day) for day in days}
    return rows, supported, exclusions, verified


def _row(document: dict, case: dict, day: str, index: int) -> dict:
    weekend = date_parts(date.fromisoformat(day))["is_weekend"]
    if case.get("weekend_mismatch") and index == 0:
        weekend = not weekend
    holiday = False
    name = None
    if case.get("unknown_holiday") and index == 0:
        holiday = True
        name = None
    temperature = "26.1"
    precipitation = "0"
    if case.get("negative") and index == 0:
        precipitation = "-1"
    if index < case.get("null_weather", 0):
        temperature = None
        precipitation = None
    return {
        "date": day,
        "location_id": document["location_id"],
        "is_public_holiday": holiday,
        "holiday_name": name,
        "is_weekend": weekend,
        "temperature_mean_c": temperature,
        "precipitation_mm": precipitation,
    }


def _exclusions(document: dict, case: dict, location_id: str, days: list[str]) -> list[dict]:
    public_count = case.get("public_exclusions", 0)
    silent_count = case.get("silent_exclusions", 0)
    public = []
    if public_count:
        public = [
            {
                "location_id": location_id,
                "date": day,
                "public": True,
                "reason": document["public_exception_reason"],
            }
            for day in days[-public_count:]
        ]
        public.append(
            {
                "location_id": "ph-baguio",
                "date": days[0],
                "public": True,
                "reason": document["public_exception_reason"],
            }
        )
    silent = []
    if silent_count:
        silent = [
            {"location_id": location_id, "date": day, "public": False, "reason": "hidden"}
            for day in days[-silent_count:]
        ]
    return public + silent


def _snapshot_sql() -> str:
    return """
    SELECT COALESCE(json_agg(json_build_object(
      'date', date::text,
      'is_public_holiday', is_public_holiday,
      'holiday_name', holiday_name
    )), '[]'::json)::text
    FROM staging.int_holiday_calendar_snapshot;
    """


def _candidate_sql() -> str:
    return """
    SELECT COALESCE(json_agg(json_build_object(
      'date', date::text,
      'location_id', location_id,
      'is_public_holiday', is_public_holiday,
      'holiday_name', holiday_name,
      'is_weekend', is_weekend,
      'temperature_mean_c', temperature_mean_c,
      'precipitation_mm', precipitation_mm
    )), '[]'::json)::text
    FROM staging.daily_external_factors_ph_candidate;
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
