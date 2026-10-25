import json

import pytest
from ops.local_stack import docker_available
from ops.migrate import MIGRATIONS, apply, psql
from ops.source_registry_seed import (
    RETENTION_FIXTURE,
    SOURCE_FIXTURE,
    load_json,
    source_rows,
)

PROBE_RUN = "00000000-0000-4000-8000-000000000008"
PROBE_ARTIFACT = "00000000-0000-4000-8000-000000000009"
PROBE_SHA = "ab" * 32


def test_migration_sql_stores_runs_manifests_and_license_decision() -> None:
    text = (MIGRATIONS / "0001_control_plane.sql").read_text(encoding="utf-8")
    for snippet in (
        "run_id UUID PRIMARY KEY",
        "interval_start DATE NOT NULL",
        "interval_end DATE NOT NULL",
        "status TEXT NOT NULL",
        "rows_received BIGINT NOT NULL",
        "rows_accepted BIGINT NOT NULL",
        "error_category TEXT",
        "started_at TIMESTAMPTZ",
        "finished_at TIMESTAMPTZ",
        "sha256_hex CHAR(64) NOT NULL",
        "retrieved_at TIMESTAMPTZ NOT NULL",
        "redistribution_status TEXT NOT NULL",
        "commercial_use_allowed BOOLEAN",
        "license_reference_url TEXT",
        "raw_manifests is immutable",
        "GRANT SELECT, INSERT ON TABLE control.raw_manifests TO of_ingest;",
    ):
        assert snippet in text


def test_seed_rows_record_the_approved_license_decision() -> None:
    rows = source_rows(load_json(SOURCE_FIXTURE), load_json(RETENTION_FIXTURE))
    by_id = {row["source_id"]: row for row in rows}
    weather = by_id["open-meteo-era5"]
    holiday = by_id["ph-official-gazette"]
    assert weather["source_url"] == "https://archive-api.open-meteo.com/v1/archive"
    assert weather["license_reference_url"] == "https://open-meteo.com/en/license"
    assert weather["source_grain"] == "location_id, Asia/Manila date"
    assert weather["attribution_text"] == "Weather data by Open-Meteo.com"
    assert holiday["source_url"] == "https://www.officialgazette.gov.ph/"
    assert holiday["license_reference_url"] == "https://www.officialgazette.gov.ph/"
    assert holiday["source_grain"] == "national Asia/Manila date"
    for row in rows:
        assert row["redistribution_status"] == "approved"
        assert row["commercial_use_allowed"] is False
        assert row["public_export_allowed"] is True
        assert row["retention_mode"] == "until_source_withdrawn"
        assert row["raw_retention_days"] is None
        assert row["approved_by"] == "Founder / Product Lead"
        assert row["approved_at"] == "2026-10-24T00:00:00+08:00"


def test_unknown_commercial_use_is_rejected() -> None:
    registry = load_json(SOURCE_FIXTURE)
    registry["sources"][0]["commercial_use"] = "sold"
    with pytest.raises(ValueError, match="commercial-use"):
        source_rows(registry, load_json(RETENTION_FIXTURE))


def test_dev_database_persists_a_run_and_rejects_manifest_changes() -> None:
    if not docker_available():
        pytest.skip("Docker is required to apply the OF-008 migration.")
    apply("dev")
    stored = psql(
        "dev",
        "of_admin",
        """
        SELECT COALESCE(json_agg(row_to_json(s) ORDER BY s.source_id), '[]'::json)
        FROM (
          SELECT source_id, redistribution_status, commercial_use_allowed,
                 public_export_allowed, retention_mode, raw_retention_days,
                 license_reference_url, attribution_text, source_grain, source_url,
                 approved_by
          FROM control.source_registry
        ) s;
        """,
        tuples_only=True,
    )
    assert stored.returncode == 0, stored.stderr
    database_rows = {row["source_id"]: row for row in json.loads(stored.stdout)}
    expected = source_rows(load_json(SOURCE_FIXTURE), load_json(RETENTION_FIXTURE))
    for row in expected:
        actual = database_rows[row["source_id"]]
        for key in (
            "redistribution_status",
            "commercial_use_allowed",
            "public_export_allowed",
            "retention_mode",
            "license_reference_url",
            "attribution_text",
            "source_grain",
            "source_url",
            "approved_by",
        ):
            assert actual[key] == row[key]
        assert actual["raw_retention_days"] is None

    probe = psql("dev", "of_admin", _probe_sql())
    assert probe.returncode == 0, probe.stderr or probe.stdout
    stored_by_ingest = psql("dev", "of_ingest", _ingest_insert_sql())
    assert stored_by_ingest.returncode == 0, stored_by_ingest.stderr or stored_by_ingest.stdout

    denied = psql("dev", "of_api", "SELECT 1 FROM control.raw_manifests;")
    assert denied.returncode != 0


def _ingest_insert_sql() -> str:
    return f"""
    BEGIN;
    INSERT INTO control.ingestion_runs (
      run_id, source_id, stream, location_id, interval_start, interval_end,
      code_revision, status, rows_received, rows_accepted, error_category,
      started_at, finished_at
    ) VALUES (
      '{PROBE_RUN}',
      'open-meteo-era5',
      'weather',
      'ph-manila',
      DATE '2018-01-01',
      DATE '2018-01-01',
      'of-008',
      'failed',
      2,
      1,
      'upstream_http',
      TIMESTAMPTZ '2026-10-24 00:00:00+08',
      TIMESTAMPTZ '2026-10-24 00:00:01+08'
    );
    INSERT INTO control.raw_manifests (
      artifact_id, run_id, source_id, object_key, sha256_hex, content_type,
      request_fingerprint, retrieved_at, response_status, bytes_stored
    ) VALUES (
      '{PROBE_ARTIFACT}',
      '{PROBE_RUN}',
      'open-meteo-era5',
      'raw/of-008/ingest-probe',
      '{PROBE_SHA}',
      'application/json',
      'of-008-ingest-probe',
      TIMESTAMPTZ '2026-10-24 00:00:01+08',
      502,
      2
    );
    DO $$
    DECLARE
      found_count INTEGER;
    BEGIN
      SELECT COUNT(*) INTO found_count
      FROM control.ingestion_runs
      WHERE run_id = '{PROBE_RUN}'
        AND status = 'failed'
        AND rows_received = 2
        AND rows_accepted = 1
        AND error_category = 'upstream_http';
      IF found_count <> 1 THEN
        RAISE EXCEPTION 'ingest role could not store the run';
      END IF;
    END $$;
    ROLLBACK;
    """


def _probe_sql() -> str:
    return f"""
    BEGIN;
    INSERT INTO control.ingestion_runs (
      run_id, source_id, stream, location_id, interval_start, interval_end,
      code_revision, status, rows_received, rows_accepted, error_category,
      started_at, finished_at
    ) VALUES (
      '{PROBE_RUN}',
      'open-meteo-era5',
      'weather',
      'ph-manila',
      DATE '2018-01-01',
      DATE '2018-01-01',
      'of-008',
      'succeeded',
      1,
      1,
      NULL,
      TIMESTAMPTZ '2026-10-24 00:00:00+08',
      TIMESTAMPTZ '2026-10-24 00:00:01+08'
    );
    INSERT INTO control.raw_manifests (
      artifact_id, run_id, source_id, object_key, sha256_hex, content_type,
      request_fingerprint, retrieved_at, source_updated_at, response_status,
      bytes_stored
    ) VALUES (
      '{PROBE_ARTIFACT}',
      '{PROBE_RUN}',
      'open-meteo-era5',
      'raw/of-008/probe',
      '{PROBE_SHA}',
      'application/json',
      'of-008-probe',
      TIMESTAMPTZ '2026-10-24 00:00:01+08',
      NULL,
      200,
      2
    );
    DO $$
    DECLARE
      found_count INTEGER;
    BEGIN
      SELECT COUNT(*) INTO found_count
      FROM control.ingestion_runs
      WHERE run_id = '{PROBE_RUN}'
        AND status = 'succeeded'
        AND rows_received = 1
        AND rows_accepted = 1
        AND error_category IS NULL
        AND interval_start = DATE '2018-01-01';
      IF found_count <> 1 THEN
        RAISE EXCEPTION 'run was not stored';
      END IF;
      SELECT COUNT(*) INTO found_count
      FROM control.raw_manifests
      WHERE artifact_id = '{PROBE_ARTIFACT}'
        AND sha256_hex = '{PROBE_SHA}';
      IF found_count <> 1 THEN
        RAISE EXCEPTION 'manifest was not stored';
      END IF;
    END $$;
    DO $$
    BEGIN
      UPDATE control.raw_manifests
      SET bytes_stored = 0
      WHERE artifact_id = '{PROBE_ARTIFACT}';
      RAISE EXCEPTION 'manifest update was accepted';
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLERRM NOT LIKE '%raw_manifests is immutable%' THEN
          RAISE;
        END IF;
    END $$;
    DO $$
    BEGIN
      DELETE FROM control.raw_manifests
      WHERE artifact_id = '{PROBE_ARTIFACT}';
      RAISE EXCEPTION 'manifest delete was accepted';
    EXCEPTION
      WHEN OTHERS THEN
        IF SQLERRM NOT LIKE '%raw_manifests is immutable%' THEN
          RAISE;
        END IF;
    END $$;
    ROLLBACK;
    """
