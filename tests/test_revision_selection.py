"""OF-019 one weather revision per location-day, and one national holiday broadcast."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from ops.local_stack import ENVIRONMENTS, dbt_executable, docker_available, load_secrets, up
from ops.migrate import apply, psql

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM = ROOT / "transform"
CASES_PATH = ROOT / "contracts" / "fixtures" / "revision_cases.json"
RUN_WEATHER = "00000000-0000-4000-8000-000000000190"
RUN_HOLIDAY = "00000000-0000-4000-8000-000000000199"
ARTIFACTS = {
    "older_upstream": "00000000-0000-4000-8000-000000000191",
    "newer_upstream": "00000000-0000-4000-8000-000000000192",
    "later_retrieval_only": "00000000-0000-4000-8000-000000000193",
    "negative_later": "00000000-0000-4000-8000-000000000194",
    "tie_low": "00000000-0000-4000-8000-000000000195",
    "tie_high": "00000000-0000-4000-8000-000000000196",
    "old_calendar": "00000000-0000-4000-8000-000000000197",
    "new_calendar": "00000000-0000-4000-8000-000000000198",
}
APPROVED_LOCATION_IDS = (
    "ph-baguio",
    "ph-cebu-city",
    "ph-davao-city",
    "ph-manila",
    "ph-tacloban",
)
# The dev database holds the stored ERA5 history. This check needs a warehouse
# whose only weather rows are the fixtures below.
ENVIRONMENT = "test"


def _cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _weather(day: str, temperature: float | None, precipitation: float) -> dict:
    return {
        "timezone": "Asia/Manila",
        "utc_offset_seconds": 28800,
        "daily_units": {
            "time": "iso8601",
            "temperature_2m_mean": "°C",
            "precipitation_sum": "mm",
        },
        "daily": {
            "time": [day],
            "temperature_2m_mean": [temperature],
            "precipitation_sum": [precipitation],
        },
    }


def _records() -> dict[str, tuple[str, str | None, dict, str, str | None]]:
    return {
        "older_upstream": (
            "open-meteo-era5",
            "ph-manila",
            _weather("2018-01-04", 20.0, 1.0),
            "2026-03-01 00:00:00+08",
            "2026-02-01 00:00:00+08",
        ),
        "newer_upstream": (
            "open-meteo-era5",
            "ph-manila",
            _weather("2018-01-04", 21.0, 0.0),
            "2026-03-15 00:00:00+08",
            "2026-04-01 00:00:00+08",
        ),
        "later_retrieval_only": (
            "open-meteo-era5",
            "ph-manila",
            _weather("2018-01-04", 22.0, 0.0),
            "2026-05-01 00:00:00+08",
            None,
        ),
        "negative_later": (
            "open-meteo-era5",
            "ph-manila",
            _weather("2018-01-04", 23.0, -1.0),
            "2026-07-02 00:00:00+08",
            "2026-07-01 00:00:00+08",
        ),
        "tie_low": (
            "open-meteo-era5",
            "ph-manila",
            _weather("2018-01-05", 10.0, 0.0),
            "2099-08-01 00:00:00+08",
            None,
        ),
        "tie_high": (
            "open-meteo-era5",
            "ph-manila",
            _weather("2018-01-05", 11.0, 0.0),
            "2099-08-01 00:00:00+08",
            None,
        ),
        "old_calendar": (
            "ph-official-gazette",
            None,
            {
                "declared_status": "complete",
                "observances": [
                    {
                        "name": "Alpha Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2018-01-04",
                    }
                ],
            },
            "2026-01-02 00:00:00+08",
            "2026-01-01 00:00:00+08",
        ),
        "new_calendar": (
            "ph-official-gazette",
            None,
            {
                "declared_status": "complete",
                "observances": [
                    {
                        "name": "Beta Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2018-01-06",
                    },
                    {
                        "name": "Alpha Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2018-01-06",
                    },
                    {
                        "name": "Alpha Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2018-01-06",
                    },
                    {
                        "name": "Local Example Day",
                        "category": "local",
                        "scope": "local",
                        "date": "2018-01-06",
                    },
                ],
            },
            "2026-06-02 00:00:00+08",
            "2026-06-01 00:00:00+08",
        ),
    }


def _digest(name: str) -> str:
    payload = _records()[name][2]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _database_ready() -> bool:
    if not docker_available():
        return False
    port = int(ENVIRONMENTS[ENVIRONMENT]["postgres_port"])
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _ensure_isolated_database() -> None:
    if not docker_available():
        pytest.skip("Docker is required to select revisions.")
    if _database_ready():
        return
    up(ENVIRONMENT)
    if not _database_ready():
        pytest.fail("The isolated test database did not start.")


def _dbt_env() -> dict[str, str]:
    secrets = load_secrets(ENVIRONMENT)
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


def _seed_sql() -> str:
    weather_manifests: list[str] = []
    holiday_manifests: list[str] = []
    landed_rows: list[str] = []
    records = _records().items()
    for name, (source_id, location_id, payload, retrieved_at, source_updated_at) in records:
        body = json.dumps(payload).encode()
        updated = "NULL" if source_updated_at is None else f"TIMESTAMPTZ '{source_updated_at}'"
        location_sql = "NULL" if location_id is None else f"'{location_id}'"
        run_id = RUN_HOLIDAY if source_id == "ph-official-gazette" else RUN_WEATHER
        row = (
            f"('{ARTIFACTS[name]}', '{run_id}', '{source_id}', 'raw/of-019/{name}', "
            f"'{_digest(name)}', 'application/json', 'of-019-{name}', "
            f"TIMESTAMPTZ '{retrieved_at}', {updated}, 200, {len(body)})"
        )
        if source_id == "ph-official-gazette":
            holiday_manifests.append(row)
        else:
            weather_manifests.append(row)
        landed_rows.append(
            f"('{ARTIFACTS[name]}', '{source_id}', {location_sql}, "
            f"$payload${json.dumps(payload)}$payload$::jsonb)"
        )
    return f"""
    INSERT INTO control.ingestion_runs (
      run_id, source_id, stream, location_id, interval_start, interval_end,
      code_revision, status, rows_received, rows_accepted, started_at, finished_at
    ) VALUES
      (
        '{RUN_WEATHER}', 'open-meteo-era5', 'daily', 'ph-manila',
        DATE '2018-01-04', DATE '2018-01-05', 'of-019', 'succeeded', 6, 6,
        TIMESTAMPTZ '2026-08-01 00:00:00+08', TIMESTAMPTZ '2026-08-01 00:00:01+08'
      ),
      (
        '{RUN_HOLIDAY}', 'ph-official-gazette', 'calendar', NULL,
        DATE '2018-01-01', DATE '2018-12-31', 'of-019', 'succeeded', 2, 2,
        TIMESTAMPTZ '2026-06-02 00:00:00+08', TIMESTAMPTZ '2026-06-02 00:00:01+08'
      );
    INSERT INTO control.raw_manifests (
      artifact_id, run_id, source_id, object_key, sha256_hex, content_type,
      request_fingerprint, retrieved_at, source_updated_at, response_status, bytes_stored
    ) VALUES
      {", ".join(weather_manifests + holiday_manifests)};
    INSERT INTO staging.landed_payload (artifact_id, source_id, location_id, payload) VALUES
      {", ".join(landed_rows)};
    """


def _cleanup() -> None:
    artifact_list = ", ".join(f"'{artifact_id}'" for artifact_id in ARTIFACTS.values())
    completed = psql(
        ENVIRONMENT,
        "of_admin",
        f"""
        BEGIN;
        ALTER TABLE control.raw_manifests DISABLE TRIGGER raw_manifests_immutable;
        DELETE FROM staging.landed_payload WHERE artifact_id IN ({artifact_list});
        DELETE FROM control.raw_manifests WHERE run_id IN ('{RUN_WEATHER}', '{RUN_HOLIDAY}');
        DELETE FROM control.ingestion_runs WHERE run_id IN ('{RUN_WEATHER}', '{RUN_HOLIDAY}');
        ALTER TABLE control.raw_manifests ENABLE TRIGGER raw_manifests_immutable;
        COMMIT;
        """,
    )
    assert completed.returncode == 0, completed.stderr


def _rows(sql: str) -> list[str]:
    completed = psql(ENVIRONMENT, "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]


def test_revision_fixture_rejects_a_publication_calendar() -> None:
    cases = _cases()
    assert cases["not_a_proclamation"] is True
    assert cases["revision_order"] == [
        "source_updated_at DESC NULLS LAST",
        "retrieved_at DESC",
        "artifact_id DESC",
    ]


def test_one_weather_revision_and_one_holiday_broadcast() -> None:
    if not dbt_executable().is_file():
        pytest.skip("dbt is not installed in the project environment.")
    _ensure_isolated_database()
    cases = _cases()
    apply(ENVIRONMENT)
    env = _dbt_env()
    _cleanup()
    try:
        seeded = psql(ENVIRONMENT, "of_admin", _seed_sql())
        assert seeded.returncode == 0, seeded.stderr
        first = _run_dbt(env)
        assert first.returncode == 0, first.stdout + first.stderr
        weather = cases["weather"]
        later = weather["later_source_update"]
        tie = weather["artifact_id_tie_break"]
        holiday = cases["holiday"]
        selected = _rows(
            """
            SELECT date::text, temperature_mean_c::text, precipitation_mm::text, source_revision
            FROM staging.int_weather_selected_revision
            WHERE location_id = 'ph-manila'
              AND date IN (DATE '2018-01-04', DATE '2018-01-05')
            ORDER BY date;
            """
        )
        assert [_measure(line) for line in selected] == [
            _expected_measure(later),
            _expected_measure(tie),
        ]
        broadcast = _rows(
            f"""
            SELECT location_id, holiday_name, holiday_count::text,
                   CASE WHEN is_public_holiday THEN 't' ELSE 'f' END,
                   CASE WHEN is_weekend THEN 't' ELSE 'f' END,
                   holiday_source_revision
            FROM staging.int_location_day
            WHERE date = DATE '{holiday["broadcast_date"]}'
            ORDER BY location_id;
            """
        )
        assert broadcast == [
            (
                f"{location_id}|{holiday['holiday_name']}|{holiday['holiday_count']}|"
                f"t|{'t' if holiday['is_weekend'] else 'f'}|{_digest(holiday['winning_snapshot'])}"
            )
            for location_id in APPROVED_LOCATION_IDS
        ]
        absent = holiday["absent_from_winner"]
        january_fourth = _rows(
            f"""
            SELECT location_id,
                   COALESCE(temperature_mean_c::text, ''),
                   COALESCE(precipitation_mm::text, ''),
                   CASE WHEN is_public_holiday THEN 't' ELSE 'f' END,
                   COALESCE(holiday_name, '')
            FROM staging.int_location_day
            WHERE date = DATE '{absent["date"]}'
            ORDER BY location_id;
            """
        )
        assert [line.split("|", 1)[0] for line in january_fourth] == list(APPROVED_LOCATION_IDS)
        for line in january_fourth:
            location_id, temperature, precipitation, flag, name = line.split("|")
            assert flag == "f"
            assert name == ""
            if location_id == weather["location_id"]:
                assert Decimal(temperature) == Decimal(later["temperature_mean_c"])
                assert Decimal(precipitation) == Decimal(later["precipitation_mm"])
            else:
                assert temperature == ""
                assert precipitation == ""
        year_rows = _rows(
            """
            SELECT
              (SELECT count(*) FROM staging.int_location_day WHERE date >= DATE '2018-01-01'
                 AND date < DATE '2019-01-01')::text,
              (
                (SELECT count(*) FROM staging.dim_location)
                * (SELECT count(*) FROM staging.dim_date WHERE calendar_year = 2018)
              )::text,
              (SELECT count(*) FROM staging.int_location_day
                 WHERE date >= DATE '2019-01-01' AND date < DATE '2020-01-01')::text;
            """
        )
        joined, expected, outside = year_rows[0].split("|")
        assert joined == expected
        assert outside == "0"
        assert int(joined) == len(APPROVED_LOCATION_IDS) * 365
        gold = {
            line.split("|", 1)[0]: line
            for line in _rows(
                """
                SELECT location_id,
                       COALESCE(temperature_mean_c::text, ''),
                       COALESCE(precipitation_mm::text, ''),
                       CASE WHEN is_public_holiday THEN 't' ELSE 'f' END,
                       COALESCE(holiday_name, ''),
                       weather_source_id,
                       holiday_source_id,
                       dataset_version,
                       weather_data_status,
                       COALESCE(release_id::text, ''),
                       CASE
                         WHEN source_updated_at = TIMESTAMPTZ '2026-06-01 00:00:00+08' THEN 't'
                         ELSE 'f'
                       END
                FROM staging.daily_external_factors_ph_candidate
                WHERE date = DATE '2018-01-04'
                  AND location_id IN ('ph-baguio', 'ph-manila')
                ORDER BY location_id;
                """
            )
        }
        _assert_gold(gold["ph-baguio"], "", "", "missing")
        _assert_gold(
            gold["ph-manila"],
            later["temperature_mean_c"],
            later["precipitation_mm"],
            "available",
        )
        holiday_row = _rows(
            """
            SELECT holiday_name, weather_data_status
            FROM staging.daily_external_factors_ph_candidate
            WHERE date = DATE '2018-01-06' AND location_id = 'ph-manila';
            """
        )
        stored_history = _rows(
            "SELECT count(*)::text FROM control.raw_manifests "
            "WHERE request_fingerprint LIKE 'stored-open-meteo-era5-%';"
        )
        weather_status = "available" if stored_history == ["5"] else "missing"
        assert holiday_row == [f"{holiday['holiday_name']}|{weather_status}"]
        second = _run_dbt(env)
        assert second.returncode == 0, second.stdout + second.stderr
        again = _rows(
            """
            SELECT date::text, source_revision
            FROM staging.int_weather_selected_revision
            WHERE location_id = 'ph-manila'
              AND date IN (DATE '2018-01-04', DATE '2018-01-05')
            ORDER BY date;
            """
        )
        assert again == [
            f"{later['date']}|{_digest(later['winner'])}",
            f"{tie['date']}|{_digest(tie['winner'])}",
        ]
    finally:
        _cleanup()


def _assert_gold(line: str, temperature: str, precipitation: str, status: str) -> None:
    (
        _location_id,
        stored_temperature,
        stored_precipitation,
        flag,
        name,
        weather_source,
        holiday_source,
        version,
        stored_status,
        release_id,
        updated,
    ) = line.split("|")
    assert flag == "f"
    assert name == ""
    assert weather_source == "open-meteo-era5"
    assert holiday_source == "ph-official-gazette"
    assert version == "1.0.0"
    assert stored_status == status
    assert release_id == ""
    assert updated == "t"
    if temperature == "":
        assert stored_temperature == ""
        assert stored_precipitation == ""
    else:
        assert Decimal(stored_temperature) == Decimal(temperature)
        assert Decimal(stored_precipitation) == Decimal(precipitation)


def _measure(line: str) -> tuple[str, Decimal, Decimal, str]:
    day, temperature, precipitation, revision = line.split("|")
    return day, Decimal(temperature), Decimal(precipitation), revision


def _expected_measure(case: dict) -> tuple[str, Decimal, Decimal, str]:
    return (
        case["date"],
        Decimal(case["temperature_mean_c"]),
        Decimal(case["precipitation_mm"]),
        _digest(case["winner"]),
    )
