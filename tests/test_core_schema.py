"""OF-016 dimension and source-fact constraints."""

import pytest
from ops.local_stack import docker_available
from ops.migrate import MIGRATIONS, apply, psql

RUN_ID = "00000000-0000-4000-8000-000000000016"
ARTIFACT_A = "00000000-0000-4000-8000-000000000017"
ARTIFACT_B = "00000000-0000-4000-8000-000000000018"


def test_migration_sql_declares_dimension_and_fact_constraints() -> None:
    text = (MIGRATIONS / "0004_core_dimensions.sql").read_text(encoding="utf-8")
    for snippet in (
        "PRIMARY KEY (location_id, date, source_revision)",
        "PRIMARY KEY (date, holiday_id, source_revision)",
        "REFERENCES control.source_registry(source_id)",
        "REFERENCES control.raw_manifests(artifact_id)",
        "CREATE INDEX idx_weather_source_date",
        "CREATE INDEX idx_holidays_date",
        "location geometry is immutable",
        "daily_external_factors_ph_location_fkey",
        "daily_external_factors_ph_weather_source_fkey",
        "holiday_category IN ('regular', 'special_non_working')",
        "EXTRACT(ISODOW FROM date)",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE",
    ):
        assert snippet in text


def test_dev_database_enforces_dimensions_revisions_and_sources() -> None:
    if not docker_available():
        pytest.skip("Docker is required to apply the OF-016 migration.")
    apply("dev")
    privileges = psql(
        "dev",
        "of_admin",
        """
        SELECT has_table_privilege('of_transform', 'core.fact_daily_weather', 'INSERT'),
               has_table_privilege('of_api', 'core.dim_location', 'SELECT'),
               has_table_privilege('of_ingest', 'core.fact_daily_weather', 'INSERT');
        """,
        tuples_only=True,
    )
    assert privileges.returncode == 0, privileges.stderr
    assert privileges.stdout.strip() == "t|f|f"
    denied = psql("dev", "of_api", "SELECT * FROM core.dim_location;")
    assert denied.returncode != 0
    assert "permission denied" in denied.stderr
    probe = psql("dev", "of_admin", _probe_sql())
    assert probe.returncode == 0, probe.stderr


def _expect_failure(statement: str, message: str) -> str:
    return f"""
    DO $$
    BEGIN
      {statement}
      RAISE EXCEPTION 'constraint accepted a forbidden row';
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLERRM NOT LIKE '%{message}%' THEN
          RAISE;
        END IF;
    END $$;
    """


def _probe_sql() -> str:
    return f"""
    BEGIN;
    INSERT INTO core.dim_location (
      location_id, display_name, country_code, admin_region, latitude, longitude,
      timezone, provider_location_ref, weather_grid_description, valid_from,
      approved_for_mvp
    ) VALUES (
      'ph-manila', 'Manila', 'PH', 'National Capital Region', 14.590600, 120.979900,
      'Asia/Manila', '14.50,121.00',
      'ERA5 0.25 degree cell, about 25 km. Reanalysis grid, not a station.',
      DATE '2018-01-01', TRUE
    )
    ON CONFLICT (location_id) DO NOTHING;
    {
        _expect_failure(
            "INSERT INTO core.dim_date "
            "(date, day_of_week, is_weekend, calendar_year, calendar_month) "
            "VALUES (DATE '2018-01-01', 2, FALSE, 2018, 1);",
            "check constraint",
        )
    }
    INSERT INTO core.dim_date (
      date, day_of_week, is_weekend, calendar_year, calendar_month
    ) VALUES (
      DATE '2018-01-01', 1, FALSE, 2018, 1
    )
    ON CONFLICT (date) DO NOTHING;
    INSERT INTO control.ingestion_runs (
      run_id, source_id, stream, location_id, interval_start, interval_end,
      code_revision, status, rows_received, rows_accepted, started_at, finished_at
    ) VALUES (
      '{RUN_ID}', 'open-meteo-era5', 'daily', 'ph-manila',
      DATE '2018-01-01', DATE '2018-01-01', 'of-016', 'succeeded', 2, 2,
      TIMESTAMPTZ '2026-10-24 00:00:00+08', TIMESTAMPTZ '2026-10-24 00:00:01+08'
    );
    INSERT INTO control.raw_manifests (
      artifact_id, run_id, source_id, object_key, sha256_hex, content_type,
      request_fingerprint, retrieved_at, source_updated_at, response_status, bytes_stored
    ) VALUES
      (
        '{ARTIFACT_A}', '{RUN_ID}', 'open-meteo-era5', 'raw/of-016/rev-a',
        '{"ab" * 32}', 'application/json', 'of-016-rev-a',
        TIMESTAMPTZ '2026-10-24 00:00:01+08', NULL, 200, 2
      ),
      (
        '{ARTIFACT_B}', '{RUN_ID}', 'open-meteo-era5', 'raw/of-016/rev-b',
        '{"cd" * 32}', 'application/json', 'of-016-rev-b',
        TIMESTAMPTZ '2026-10-24 00:00:02+08', NULL, 200, 2
      );
    INSERT INTO core.fact_daily_weather (
      location_id, date, source_revision, source_id, artifact_id,
      temperature_mean_c, precipitation_mm, weather_data_status,
      source_updated_at, retrieved_at
    ) VALUES
      (
        'ph-manila', DATE '2018-01-01', 'rev-a', 'open-meteo-era5', '{ARTIFACT_A}',
        26.100, 3.400, 'available', NULL, TIMESTAMPTZ '2026-10-24 00:00:01+08'
      ),
      (
        'ph-manila', DATE '2018-01-01', 'rev-b', 'open-meteo-era5', '{ARTIFACT_B}',
        26.200, 3.500, 'available', NULL, TIMESTAMPTZ '2026-10-24 00:00:02+08'
      );
    DO $$
    DECLARE
      found_count INTEGER;
    BEGIN
      SELECT COUNT(*) INTO found_count
      FROM core.fact_daily_weather
      WHERE location_id = 'ph-manila' AND date = DATE '2018-01-01';
      IF found_count <> 2 THEN
        RAISE EXCEPTION 'two source revisions were not stored';
      END IF;
    END $$;
    {
        _expect_failure(
            "INSERT INTO core.fact_daily_weather ("
            "location_id, date, source_revision, source_id, artifact_id, "
            "temperature_mean_c, precipitation_mm, weather_data_status, retrieved_at"
            ") VALUES ("
            "'ph-manila', DATE '2018-01-01', 'rev-c', 'not-a-source', "
            f"'{ARTIFACT_A}', 1, 0, 'available', TIMESTAMPTZ '2026-10-24 00:00:03+08');",
            "foreign key",
        )
    }
    {
        _expect_failure(
            "INSERT INTO core.fact_daily_weather ("
            "location_id, date, source_revision, source_id, artifact_id, "
            "temperature_mean_c, precipitation_mm, weather_data_status, retrieved_at"
            ") VALUES ("
            "'ph-manila', DATE '2018-01-01', 'rev-c', 'open-meteo-era5', "
            f"'{ARTIFACT_A}', 1, -1, 'available', TIMESTAMPTZ '2026-10-24 00:00:03+08');",
            "check constraint",
        )
    }
    {
        _expect_failure(
            "INSERT INTO core.fact_public_holidays ("
            "date, holiday_id, source_revision, source_id, artifact_id, holiday_name, "
            "holiday_category, scope, retrieved_at"
            ") VALUES ("
            "DATE '2018-01-01', 'local-example', 'rev-a', 'ph-official-gazette', "
            f"'{ARTIFACT_A}', 'Local Example Day', 'local', 'local', "
            "TIMESTAMPTZ '2026-10-24 00:00:03+08');",
            "check constraint",
        )
    }
    {
        _expect_failure(
            "UPDATE core.dim_location SET latitude = 0 WHERE location_id = 'ph-manila';",
            "location geometry is immutable",
        )
    }
    {
        _expect_failure(
            "INSERT INTO publish.daily_external_factors_ph ("
            "release_id, date, location_id, country_code, is_public_holiday, holiday_name, "
            "is_weekend, weather_source_id, holiday_source_id, dataset_version, weather_data_status"
            ") VALUES ("
            "'00000000-0000-4000-8000-000000000019', DATE '2018-01-01', 'ph-missing', 'PH', "
            "FALSE, NULL, FALSE, 'open-meteo-era5', 'ph-official-gazette', '1.0.0', 'missing');",
            "foreign key",
        )
    }
    ROLLBACK;
    """
