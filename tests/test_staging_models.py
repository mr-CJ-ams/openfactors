"""OF-017 staging parse, quarantine, and deterministic dbt rebuild."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from ops.local_stack import dev_database_ready, load_secrets
from ops.migrate import apply, psql

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM = ROOT / "transform"
RUN_ID = "00000000-0000-4000-8000-000000000171"
ARTIFACTS = {
    "weather": "00000000-0000-4000-8000-000000000172",
    "shape": "00000000-0000-4000-8000-000000000173",
    "negative": "00000000-0000-4000-8000-000000000174",
    "timezone": "00000000-0000-4000-8000-000000000175",
    "holiday": "00000000-0000-4000-8000-000000000176",
    "shape_holiday": "00000000-0000-4000-8000-000000000177",
    "incomplete": "00000000-0000-4000-8000-000000000178",
}


def _weather_payload(timezone: str, precipitation: list) -> dict:
    return {
        "timezone": timezone,
        "utc_offset_seconds": 28800,
        "daily_units": {
            "time": "iso8601",
            "temperature_2m_mean": "°C",
            "precipitation_sum": "mm",
        },
        "daily": {
            "time": ["2018-01-01"],
            "temperature_2m_mean": [26.1],
            "precipitation_sum": precipitation,
        },
    }


def _payloads() -> dict[str, tuple[str, str | None, dict]]:
    shape = _weather_payload("Asia/Manila", [0.0])
    shape["daily"]["time"] = ["2099-01-02"]
    shape["daily"]["temperature_2m_mean"] = [None]
    return {
        "weather": ("open-meteo-era5", "ph-manila", _weather_payload("Asia/Manila", [3.4])),
        "shape": ("open-meteo-era5", "ph-manila", shape),
        "negative": ("open-meteo-era5", "ph-manila", _weather_payload("Asia/Manila", [-1])),
        "timezone": ("open-meteo-era5", "ph-manila", _weather_payload("UTC", [0])),
        "holiday": (
            "ph-official-gazette",
            None,
            {
                "declared_status": "complete",
                "observances": [
                    {
                        "name": "Alpha Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2099-01-04",
                    },
                    {
                        "name": "Local Example Day",
                        "category": "local",
                        "scope": "local",
                        "date": "2099-01-06",
                    },
                ],
            },
        ),
        "shape_holiday": (
            "ph-official-gazette",
            None,
            {
                "not_a_proclamation": True,
                "declared_status": "complete",
                "observances": [
                    {
                        "name": "Alpha Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2099-01-04",
                    }
                ],
            },
        ),
        "incomplete": (
            "ph-official-gazette",
            None,
            {
                "declared_status": "incomplete",
                "observances": [
                    {
                        "name": "Alpha Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2099-01-04",
                    }
                ],
            },
        ),
    }


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


def _seed_sql() -> str:
    manifest_rows: list[str] = []
    landed_rows: list[str] = []
    for name, (source_id, location_id, payload) in _payloads().items():
        body = json.dumps(payload).encode()
        digest = hashlib.sha256(body).hexdigest()
        artifact_id = ARTIFACTS[name]
        location_sql = "NULL" if location_id is None else f"'{location_id}'"
        manifest_rows.append(
            "("
            f"'{artifact_id}', '{RUN_ID}', '{source_id}', 'raw/of-017/{name}', "
            f"'{digest}', 'application/json', 'of-017-{name}', "
            "TIMESTAMPTZ '2026-10-24 03:00:00+08', NULL, 200, "
            f"{len(body)}"
            ")"
        )
        landed_rows.append(
            f"('{artifact_id}', '{source_id}', {location_sql}, "
            f"$payload${json.dumps(payload)}$payload$::jsonb)"
        )
    return f"""
    INSERT INTO control.ingestion_runs (
      run_id, source_id, stream, location_id, interval_start, interval_end,
      code_revision, status, rows_received, rows_accepted, started_at, finished_at
    ) VALUES (
      '{RUN_ID}', 'open-meteo-era5', 'daily', 'ph-manila',
      DATE '2018-01-01', DATE '2018-01-01', 'of-017', 'succeeded', 1, 1,
      TIMESTAMPTZ '2026-10-24 03:00:00+08', TIMESTAMPTZ '2026-10-24 03:00:01+08'
    );
    INSERT INTO control.raw_manifests (
      artifact_id, run_id, source_id, object_key, sha256_hex, content_type,
      request_fingerprint, retrieved_at, source_updated_at, response_status, bytes_stored
    ) VALUES
      {", ".join(manifest_rows)};
    INSERT INTO staging.landed_payload (artifact_id, source_id, location_id, payload) VALUES
      {", ".join(landed_rows)};
    """


def _cleanup() -> None:
    completed = psql(
        "dev",
        "of_admin",
        f"""
        BEGIN;
        ALTER TABLE control.raw_manifests DISABLE TRIGGER raw_manifests_immutable;
        DELETE FROM staging.landed_payload WHERE artifact_id IN (
          {", ".join(f"'{artifact_id}'" for artifact_id in ARTIFACTS.values())}
        );
        DELETE FROM control.raw_manifests WHERE run_id = '{RUN_ID}';
        DELETE FROM control.ingestion_runs WHERE run_id = '{RUN_ID}';
        ALTER TABLE control.raw_manifests ENABLE TRIGGER raw_manifests_immutable;
        COMMIT;
        """,
    )
    assert completed.returncode == 0, completed.stderr


def _rows(sql: str) -> list[str]:
    completed = psql("dev", "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]


def test_staging_models_parse_units_and_rebuild_the_same_rows() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to build the staging models.")
    if not Path(sys.executable).with_name("dbt.exe").is_file():
        pytest.skip("dbt is not installed in the project environment.")
    apply("dev")
    env = _dbt_env()
    _cleanup()
    try:
        seeded = psql("dev", "of_admin", _seed_sql())
        assert seeded.returncode == 0, seeded.stderr
        first = _run_dbt(env)
        assert first.returncode == 0, first.stdout + first.stderr
        before = _rows(
            """
            SELECT location_id || '|' || date::text || '|' || source_revision
                || '|' || COALESCE(temperature_mean_c::text, '')
                || '|' || COALESCE(precipitation_mm::text, '')
                || '|' || weather_data_status
            FROM staging.stg_weather
            ORDER BY 1;
            """
        )
        holidays = _rows(
            f"""
            SELECT date::text || '|' || holiday_name || '|' || holiday_category
            FROM staging.stg_holidays
            WHERE artifact_id = '{ARTIFACTS["holiday"]}'
            ORDER BY 1;
            """
        )
        quarantined = set(
            _rows(
                """
                SELECT artifact_id::text || '|' || quarantine_reason
                FROM staging.stg_weather_quarantine
                UNION ALL
                SELECT artifact_id::text || '|' || quarantine_reason
                FROM staging.stg_holidays_quarantine;
                """
            )
        )
        second = _run_dbt(env)
        assert second.returncode == 0, second.stdout + second.stderr
        after = _rows(
            """
            SELECT location_id || '|' || date::text || '|' || source_revision
                || '|' || COALESCE(temperature_mean_c::text, '')
                || '|' || COALESCE(precipitation_mm::text, '')
                || '|' || weather_data_status
            FROM staging.stg_weather
            ORDER BY 1;
            """
        )
    finally:
        _cleanup()
    weather = _payloads()["weather"][2]
    digest = hashlib.sha256(json.dumps(weather).encode()).hexdigest()
    assert f"ph-manila|2018-01-01|{digest}|26.1|3.4|available" in before
    shape_rows = [line for line in before if "|2099-01-02|" in line]
    assert len(shape_rows) == 1
    assert shape_rows[0].endswith("||0.0|available")
    assert not any("|2018-01-01|" in line and ARTIFACTS["negative"] in line for line in before)
    assert holidays == ["2099-01-04|Alpha Day|regular"]
    assert f"{ARTIFACTS['negative']}|negative_precipitation" in quarantined
    assert f"{ARTIFACTS['timezone']}|wrong_timezone" in quarantined
    assert f"{ARTIFACTS['shape_holiday']}|not_a_proclamation" in quarantined
    assert f"{ARTIFACTS['incomplete']}|incomplete_year" in quarantined
    assert f"{ARTIFACTS['holiday']}|out_of_scope" in quarantined
    assert before == after
