"""OF-023 candidate release manifest, checksums, and quality evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.local_stack import docker_available, load_secrets
from ops.migrate import apply, psql
from ops.releases import persist_manifest

from openfactors.quality.gates import INTERNAL_COLUMNS, physical_type
from openfactors.quality.manifest import (
    ReleaseManifest,
    build_manifest,
    canonical_artifacts,
    canonical_rows,
    content_sha256,
    source_manifest_hash,
)

ROOT = Path(__file__).resolve().parents[1]
TRANSFORM = ROOT / "transform"
LOCATIONS_PATH = ROOT / "contracts" / "fixtures" / "pilot_locations.json"
SHA = "ab" * 32
OTHER_SHA = "cd" * 32
ARTIFACT = "00000000-0000-4000-8000-000000000191"
STAMP = "2026-10-25T00:00:00+00:00"


def test_schema_version_stays_distinct_and_checksums_follow_content() -> None:
    contract = load_json(CONTRACT_PATH)
    row = _row()
    manifest = _build(contract, [row])
    assert manifest.status == "approved"
    assert manifest.schema_version == "1.0.0"
    assert manifest.dataset_id == contract["dataset_id"]
    assert manifest.release_id != manifest.schema_version
    uuid.UUID(manifest.release_id)
    assert manifest.schema_gate.status == "PASS"
    assert manifest.coverage.status == "PASS"
    assert manifest.version_mismatch is False
    digest = hashlib.sha256(
        json.dumps(
            canonical_rows([row]),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    reordered = content_sha256([dict(reversed(list(row.items())))])
    assert manifest.content_sha256_hex == digest == reordered
    assert canonical_rows([row])[0]["temperature_mean_c"] == "26.100"
    assert canonical_rows([row])[0]["precipitation_mm"] == "3.400"
    changed = _row()
    changed["temperature_mean_c"] = "18.4"
    other = _build(contract, [changed], release_id="00000000-0000-4000-8000-000000000231")
    assert other.schema_version == manifest.schema_version
    assert other.release_id != manifest.release_id
    assert other.content_sha256_hex != manifest.content_sha256_hex
    artifacts = [
        {"artifact_id": ARTIFACT, "sha256_hex": SHA.upper(), "source_id": "open-meteo-era5"},
        {"artifact_id": ARTIFACT, "sha256_hex": SHA, "source_id": "open-meteo-era5"},
    ]
    assert source_manifest_hash(list(reversed(artifacts))) == source_manifest_hash(artifacts)
    assert canonical_artifacts(artifacts) == [
        {"artifact_id": ARTIFACT, "sha256_hex": SHA, "source_id": "open-meteo-era5"}
    ]
    swapped = source_manifest_hash(
        [{"artifact_id": ARTIFACT, "sha256_hex": OTHER_SHA, "source_id": "open-meteo-era5"}]
    )
    assert swapped != manifest.source_manifest_set_hash
    evidence = manifest.evidence()
    assert evidence["schema_version"] == "1.0.0"
    assert evidence["release_id"] == manifest.release_id
    assert evidence["content_sha256_hex"] == manifest.content_sha256_hex


def test_failed_gate_is_rejected_and_keeps_the_quality_link() -> None:
    contract = load_json(CONTRACT_PATH)
    negative = _row()
    negative["precipitation_mm"] = "-1"
    rejected = _build(contract, [negative])
    assert rejected.status == "rejected"
    assert rejected.schema_version == "1.0.0"
    assert "negative_precipitation" in rejected.coverage.violations
    assert rejected.evidence()["coverage_gate"]["status"] == "FAIL"
    assert rejected.content_sha256_hex != content_sha256([_row()])
    mismatched = _row()
    mismatched["dataset_version"] = "1.0.1"
    mismatch = _build(contract, [mismatched])
    assert mismatch.status == "rejected"
    assert mismatch.schema_version == "1.0.0"
    assert mismatch.version_mismatch is True
    assert mismatch.evidence()["schema_gate"]["status"] == "PASS"
    with pytest.raises(ValueError, match="semantic version"):
        _build(contract, [_row()], schema_version=str(uuid.uuid4()))


def test_live_manifest_is_immutable_and_publish_stays_empty() -> None:
    if not docker_available():
        pytest.skip("Docker is required to audit a release manifest.")
    if not Path(sys.executable).with_name("dbt.exe").is_file():
        pytest.skip("dbt is not installed in the project environment.")
    apply("dev")
    built = _run_dbt(_dbt_env())
    assert built.returncode == 0, built.stdout + built.stderr
    before = _rows("SELECT count(*)::text FROM publish.daily_external_factors_ph;")
    contract = load_json(CONTRACT_PATH)
    rows = json.loads(_rows(_candidate_sql())[0])
    artifacts = json.loads(_rows(_artifact_sql())[0])
    locations = set(_rows("SELECT location_id FROM core.dim_location WHERE approved_for_mvp;"))
    days = _rows("SELECT date::text FROM core.dim_date ORDER BY 1;")
    supported = {(location_id, day) for location_id in locations for day in days}
    verified = {
        item["date"]: {
            "is_public_holiday": item["is_public_holiday"],
            "holiday_name": item["holiday_name"],
        }
        for item in json.loads(_rows(_snapshot_sql())[0])
    }
    manifest = build_manifest(
        rows,
        _live_columns(contract),
        contract_fields=contract["fields"],
        locations=locations,
        dates=set(days),
        sources=set(
            _rows(
                "SELECT source_id FROM control.source_registry "
                "WHERE redistribution_status = 'approved';"
            )
        ),
        supported=supported,
        exclusions=[],
        verified_dates=verified,
        artifacts=artifacts,
        schema_version=contract["dataset_version"],
        coverage_start=days[0],
        coverage_end=days[-1],
        validated_at=STAMP,
    )
    ready = (
        manifest.schema_gate.status == "PASS"
        and manifest.coverage.status == "PASS"
        and not manifest.version_mismatch
        and not manifest.weather_status_invalid
    )
    assert manifest.status == ("approved" if ready else "rejected")
    assert manifest.schema_version == "1.0.0"
    assert manifest.release_id != manifest.schema_version
    assert content_sha256(json.loads(_rows(_candidate_sql())[0])) == manifest.content_sha256_hex
    assert source_manifest_hash(json.loads(_rows(_artifact_sql())[0])) == (
        manifest.source_manifest_set_hash
    )
    assert manifest.actual_row_count == len(rows)
    assert manifest.expected_row_count == manifest.coverage.expected
    persist_manifest("dev", manifest)
    stored = _rows(
        "SELECT dataset_id, schema_version, status, btrim(source_manifest_set_hash), "
        "btrim(content_sha256_hex), expected_row_count::text, actual_row_count::text, "
        "published_at IS NULL "
        f"FROM publish.dataset_releases WHERE release_id = '{manifest.release_id}';"
    )
    assert stored == [
        "|".join(
            (
                manifest.dataset_id,
                manifest.schema_version,
                manifest.status,
                manifest.source_manifest_set_hash,
                manifest.content_sha256_hex,
                str(manifest.expected_row_count),
                str(manifest.actual_row_count),
                "t",
            )
        )
    ]
    evidence = json.loads(
        _rows(
            "SELECT result_json::text FROM quality.validation_runs "
            f"WHERE release_id = '{manifest.release_id}';"
        )[0]
    )
    assert evidence["release_id"] == manifest.release_id
    assert evidence["schema_version"] == manifest.schema_version
    assert evidence["content_sha256_hex"] == manifest.content_sha256_hex
    assert evidence["schema_gate"]["status"] == manifest.schema_gate.status
    assert evidence["coverage_gate"]["status"] == manifest.coverage.status
    validation_status = _rows(
        f"SELECT status FROM quality.validation_runs WHERE release_id = '{manifest.release_id}';"
    )
    assert validation_status == ["PASS" if manifest.status == "approved" else "FAIL"]
    after = _rows("SELECT count(*)::text FROM publish.daily_external_factors_ph;")
    pointer = _rows(
        "SELECT count(*)::text FROM publish.active_dataset_release "
        f"WHERE release_id = '{manifest.release_id}';"
    )
    assert before == after == ["0"]
    assert pointer == ["0"]
    for statement in (
        "UPDATE publish.dataset_releases SET schema_version = '9.9.9' "
        f"WHERE release_id = '{manifest.release_id}';",
        "UPDATE publish.dataset_releases SET content_sha256_hex = repeat('a', 64) "
        f"WHERE release_id = '{manifest.release_id}';",
        f"DELETE FROM publish.dataset_releases WHERE release_id = '{manifest.release_id}';",
        "UPDATE quality.validation_runs SET status = 'PASS' "
        f"WHERE release_id = '{manifest.release_id}';",
    ):
        denied = psql("dev", "of_admin", statement)
        assert denied.returncode != 0, statement
        assert "immutable" in denied.stderr


def _row() -> dict:
    return {
        "date": "2018-01-01",
        "location_id": "ph-manila",
        "country_code": "PH",
        "temperature_mean_c": "26.1",
        "precipitation_mm": "3.4",
        "is_public_holiday": False,
        "holiday_name": None,
        "is_weekend": False,
        "weather_source_id": "open-meteo-era5",
        "holiday_source_id": "ph-official-gazette",
        "source_updated_at": None,
        "dataset_version": "1.0.0",
        "weather_data_status": "available",
    }


def _build(contract: dict, rows: list[dict], **overrides: str) -> ReleaseManifest:
    locations = {
        item["location_id"]
        for item in json.loads(LOCATIONS_PATH.read_text(encoding="utf-8"))["locations"]
        if item["approved_for_mvp"] is True
    }
    columns = [
        {"name": field["name"], "physical_type": field["postgres"], "nullable": field["nullable"]}
        for field in contract["fields"]
    ]
    columns.extend(dict(column) for column in INTERNAL_COLUMNS)
    arguments = {
        "contract_fields": contract["fields"],
        "locations": locations,
        "dates": {"2018-01-01"},
        "sources": {"open-meteo-era5", "ph-official-gazette"},
        "supported": {("ph-manila", "2018-01-01")},
        "exclusions": [],
        "verified_dates": {"2018-01-01": {"is_public_holiday": False, "holiday_name": None}},
        "artifacts": [{"artifact_id": ARTIFACT, "sha256_hex": SHA, "source_id": "open-meteo-era5"}],
        "schema_version": "1.0.0",
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
        "release_id": "00000000-0000-4000-8000-000000000230",
        "validated_at": STAMP,
    }
    arguments.update(overrides)
    return build_manifest(rows, columns, **arguments)


def _live_columns(contract: dict) -> list[dict]:
    nullable = {field["name"]: field["nullable"] for field in contract["fields"]}
    nullable.update({column["name"]: column["nullable"] for column in INTERNAL_COLUMNS})
    columns = []
    query = """
        SELECT column_name, data_type,
               COALESCE(character_maximum_length::text, ''),
               COALESCE(numeric_precision::text, ''),
               COALESCE(numeric_scale::text, '')
        FROM information_schema.columns
        WHERE table_schema = 'staging'
          AND table_name = 'daily_external_factors_ph_candidate'
        ORDER BY ordinal_position;
    """
    for line in _rows(query):
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
    SELECT COALESCE(json_agg(json_build_object(
      'date', date::text,
      'location_id', location_id,
      'country_code', btrim(country_code),
      'temperature_mean_c', temperature_mean_c,
      'precipitation_mm', precipitation_mm,
      'is_public_holiday', is_public_holiday,
      'holiday_name', holiday_name,
      'is_weekend', is_weekend,
      'weather_source_id', weather_source_id,
      'holiday_source_id', holiday_source_id,
      'source_updated_at', source_updated_at,
      'dataset_version', dataset_version,
      'weather_data_status', weather_data_status
    )), '[]'::json)::text
    FROM staging.daily_external_factors_ph_candidate;
    """


def _artifact_sql() -> str:
    return """
    SELECT COALESCE(json_agg(json_build_object(
      'artifact_id', artifact_id::text,
      'sha256_hex', btrim(sha256_hex),
      'source_id', source_id
    ) ORDER BY artifact_id), '[]'::json)::text
    FROM (
      SELECT DISTINCT manifests.artifact_id, manifests.sha256_hex, manifests.source_id
      FROM control.raw_manifests AS manifests
      JOIN (
        SELECT days.weather_artifact_id AS artifact_id
        FROM staging.int_location_day AS days
        JOIN staging.location_source_map AS pinned
          ON pinned.location_id = days.location_id
        WHERE days.holiday_source_id = pinned.holiday_source_id
          AND days.weather_artifact_id IS NOT NULL
          AND days.weather_source_id = pinned.weather_source_id
        UNION
        SELECT days.holiday_artifact_id
        FROM staging.int_location_day AS days
        JOIN staging.location_source_map AS pinned
          ON pinned.location_id = days.location_id
        WHERE days.holiday_source_id = pinned.holiday_source_id
          AND days.holiday_artifact_id IS NOT NULL
          AND (
            days.weather_artifact_id IS NULL
            OR days.weather_source_id = pinned.weather_source_id
          )
      ) AS used ON used.artifact_id = manifests.artifact_id
    ) AS pinned;
    """


def _snapshot_sql() -> str:
    return """
    SELECT COALESCE(json_agg(json_build_object(
      'date', date::text,
      'is_public_holiday', is_public_holiday,
      'holiday_name', holiday_name
    )), '[]'::json)::text
    FROM staging.int_holiday_calendar_snapshot;
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
