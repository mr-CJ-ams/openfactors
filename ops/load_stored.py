"""Load the stored holiday calendar and ERA5 files into the local dev database.

The candidate view is rebuilt from those files, and the approved release is
promoted on this computer. This is not a public release.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

from contracts.check_dataset_contract import load_json

from openfactors.ingestion.holidays import load_publication_calendar
from openfactors.quality.gates import INTERNAL_COLUMNS, physical_type
from openfactors.quality.manifest import build_manifest
from ops.local_stack import dbt_executable, dev_database_ready, load_secrets
from ops.migrate import apply, psql, sql_literal
from ops.promote import active_release_id, promote_release, rollback_release
from ops.releases import persist_manifest

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "ingestion" / "raw" / "open-meteo-era5"
CALENDAR_PATH = ROOT / "contracts" / "fixtures" / "holiday_calendar.json"
CONTRACT_PATH = ROOT / "contracts" / "fixtures" / "daily_factor_contract.json"
TRANSFORM = ROOT / "transform"
RETRIEVED_AT = "2026-10-25 20:00:00+08"
WEATHER_SOURCE = "open-meteo-era5"
HOLIDAY_SOURCE = "ph-official-gazette"


def holiday_landed_payload(document: dict) -> dict:
    """One complete calendar. An incomplete year is refused."""
    years = load_publication_calendar(document)
    incomplete = sorted(
        year for year, compiled in years.items() if compiled.effective_status != "complete"
    )
    if incomplete:
        listed = ", ".join(str(year) for year in incomplete)
        raise ValueError(f"Incomplete holiday years cannot be landed: {listed}.")
    if not years:
        raise ValueError("No holiday year is transcribed.")
    observances: list[dict[str, str | None]] = []
    for year in sorted(years):
        for item in years[year].observances:
            observances.append(
                {
                    "name": item.name,
                    "category": item.category,
                    "scope": item.scope,
                    "date": None if item.date is None else item.date.isoformat(),
                }
            )
    return {"declared_status": "complete", "observances": observances}


def weather_records(manifest: dict) -> list[dict]:
    """The stored ERA5 files named by the history manifest, with checksums checked."""
    records = []
    for item in manifest["locations"]:
        path = ROOT / item["object"]
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != item["sha256"] or len(body) != item["bytes"]:
            raise ValueError(
                f"Stored weather bytes do not match the manifest for {item['location_id']}."
            )
        records.append(
            {
                "location_id": item["location_id"],
                "body": body,
                "sha256": digest,
                "fingerprint": f"stored-{WEATHER_SOURCE}-{item['location_id']}",
                "run_id": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"openfactors:stored-run:{WEATHER_SOURCE}:{item['location_id']}",
                    )
                ),
            }
        )
    return records


def main(argv: list[str] | None = None) -> int:
    """Land the stored files and rebuild the local candidate view."""
    del argv
    if not dev_database_ready():
        raise SystemExit(
            "The local dev database is not running. Start it, then run this command again."
        )
    apply("dev")
    calendar = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    history = json.loads((HISTORY / "manifest.json").read_text(encoding="utf-8"))
    holiday_body = json.dumps(
        holiday_landed_payload(calendar),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    _land_holiday(holiday_body)
    for record in weather_records(history):
        _land_weather(record, history["start_date"], history["end_date"])
    built = _run_dbt()
    if built.returncode != 0:
        raise SystemExit(built.stdout + built.stderr)
    rows = _candidate_rows()
    if not rows:
        raise SystemExit("The candidate view has no rows.")
    manifest = _manifest(rows)
    if manifest.status != "approved":
        reasons = list(manifest.schema_gate.violations) + list(manifest.coverage.violations)
        print("candidate rejected: " + ", ".join(reasons))
        return 1
    release_id = _serve(rows, manifest)
    missing = sum(1 for row in rows if row.get("weather_data_status") == "missing")
    print(
        f"candidate rows {len(rows)}; weather-missing rows {missing}; "
        f"release {release_id} is the local active release."
    )
    print("Query publish.daily_factors.")
    print("The public release decision stays no-go.")
    return 0


def _land_holiday(body: bytes) -> None:
    digest = hashlib.sha256(body).hexdigest()
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "openfactors:stored-run:ph-official-gazette"))
    artifact_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "openfactors:stored-artifact:ph-official-gazette")
    )
    _land(
        run_id=run_id,
        artifact_id=artifact_id,
        source_id=HOLIDAY_SOURCE,
        location_id=None,
        fingerprint="stored-ph-official-gazette-years",
        body=body,
        digest=digest,
        interval_start="2018-01-01",
        interval_end="2026-12-31",
        rows=1,
    )


def _land_weather(record: dict, start: str, end: str) -> None:
    artifact_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"openfactors:stored-artifact:{record['fingerprint']}")
    )
    _land(
        run_id=record["run_id"],
        artifact_id=artifact_id,
        source_id=WEATHER_SOURCE,
        location_id=record["location_id"],
        fingerprint=record["fingerprint"],
        body=record["body"],
        digest=record["sha256"],
        interval_start=start,
        interval_end=end,
        rows=1,
    )


def _land(
    *,
    run_id: str,
    artifact_id: str,
    source_id: str,
    location_id: str | None,
    fingerprint: str,
    body: bytes,
    digest: str,
    interval_start: str,
    interval_end: str,
    rows: int,
) -> None:
    found = _rows(
        "SELECT btrim(sha256_hex) FROM control.raw_manifests "
        f"WHERE request_fingerprint = {sql_literal(fingerprint)};"
    )
    if found:
        if found[0] != digest:
            raise SystemExit(f"Stored artifact {fingerprint} already has different bytes.")
        return
    location_sql = "NULL" if location_id is None else sql_literal(location_id)
    key = f"raw/{source_id}/{run_id}/{digest}"
    encoded = sql_literal(body.hex())
    statement = f"""
    BEGIN;
    INSERT INTO control.ingestion_runs (
      run_id, source_id, stream, location_id, interval_start, interval_end,
      code_revision, status, rows_received, rows_accepted, started_at, finished_at
    ) VALUES (
      {sql_literal(run_id)}::uuid, {sql_literal(source_id)}, 'stored', {location_sql},
      DATE {sql_literal(interval_start)}, DATE {sql_literal(interval_end)},
      'stored-2026-10-25', 'succeeded', {rows}, {rows},
      TIMESTAMPTZ {sql_literal(RETRIEVED_AT)}, TIMESTAMPTZ {sql_literal(RETRIEVED_AT)}
    );
    INSERT INTO control.raw_manifests (
      artifact_id, run_id, source_id, object_key, sha256_hex, content_type,
      request_fingerprint, retrieved_at, source_updated_at, response_status, bytes_stored
    ) VALUES (
      {sql_literal(artifact_id)}::uuid, {sql_literal(run_id)}::uuid, {sql_literal(source_id)},
      {sql_literal(key)}, {sql_literal(digest)}, 'application/json',
      {sql_literal(fingerprint)}, TIMESTAMPTZ {sql_literal(RETRIEVED_AT)}, NULL, 200, {len(body)}
    );
    INSERT INTO staging.landed_payload (artifact_id, source_id, location_id, payload)
    VALUES (
      {sql_literal(artifact_id)}::uuid, {sql_literal(source_id)}, {location_sql},
      convert_from(decode({encoded}, 'hex'), 'UTF8')::jsonb
    );
    COMMIT;
    """
    completed = psql("dev", "of_admin", statement)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"Could not land {fingerprint}.\n{detail}")


def restore_withdrawn_release(release_id: str) -> None:
    """Serve the same rows again after a drill withdraws the local release.

    The withdrawn release stays withdrawn. A published copy is reused when its
    content checksum matches.
    """
    status = _rows(
        "SELECT status FROM publish.dataset_releases "
        f"WHERE release_id = {sql_literal(release_id)}::uuid;"
    )
    if status != ["withdrawn"]:
        return
    rows = json.loads(_rows(_fact_sql(release_id))[0])
    if not rows:
        return
    manifest = _manifest_for_copy(rows)
    if manifest.status != "approved":
        reasons = list(manifest.schema_gate.violations) + list(manifest.coverage.violations)
        raise SystemExit("The local release could not be restored: " + ", ".join(reasons))
    _serve(rows, manifest)


def _serve(rows: list[dict], manifest) -> str:
    stored = _rows(
        "SELECT release_id::text || '|' || status FROM publish.dataset_releases "
        f"WHERE content_sha256_hex = {sql_literal(manifest.content_sha256_hex)} "
        "AND status IN ('approved', 'published') "
        "ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END;"
    )
    if stored:
        release_id, status = stored[0].split("|", 1)
        if status == "published":
            if active_release_id("dev") != release_id:
                rollback_release("dev", release_id)
            return release_id
        promote_release("dev", replace(manifest, release_id=release_id), rows)
        return release_id
    persist_manifest("dev", manifest)
    promote_release("dev", manifest, rows)
    return manifest.release_id


def _manifest(rows: list[dict], release_id: str | None = None):
    contract = load_json(CONTRACT_PATH)
    locations = set(_rows("SELECT location_id FROM core.dim_location WHERE approved_for_mvp;"))
    days = _rows(
        "SELECT date::text FROM core.dim_date WHERE calendar_year BETWEEN 2018 AND 2026 ORDER BY 1;"
    )
    supported = {(location_id, day) for location_id in locations for day in days}
    verified = {
        item["date"]: {
            "is_public_holiday": item["is_public_holiday"],
            "holiday_name": item["holiday_name"],
        }
        for item in json.loads(_rows(_snapshot_sql())[0])
    }
    artifacts = json.loads(_rows(_artifact_sql())[0])
    start = min(row["date"] for row in rows)
    end = max(row["date"] for row in rows)
    return build_manifest(
        rows,
        _columns(contract),
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
        coverage_start=start,
        coverage_end=end,
        release_id=release_id,
    )


def _manifest_for_copy(rows: list[dict]):
    contract = load_json(CONTRACT_PATH)
    locations = {row["location_id"] for row in rows}
    days = {row["date"] for row in rows}
    verified: dict[str, dict] = {}
    for row in rows:
        verified[row["date"]] = {
            "is_public_holiday": row["is_public_holiday"],
            "holiday_name": row["holiday_name"],
        }
    artifacts = json.loads(_rows(_artifact_sql())[0])
    return build_manifest(
        rows,
        _columns(contract),
        contract_fields=contract["fields"],
        locations=locations,
        dates=days,
        sources=set(
            _rows(
                "SELECT source_id FROM control.source_registry "
                "WHERE redistribution_status = 'approved';"
            )
        ),
        supported={(row["location_id"], row["date"]) for row in rows},
        exclusions=[],
        verified_dates=verified,
        artifacts=artifacts,
        schema_version=contract["dataset_version"],
        coverage_start=min(days),
        coverage_end=max(days),
    )


def _columns(contract: dict) -> list[dict]:
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


def _candidate_rows() -> list[dict]:
    payload = _rows(
        """
        SELECT COALESCE(json_agg(row_to_json(candidate)), '[]'::json)::text
        FROM (
          SELECT date::text AS date,
                 location_id,
                 btrim(country_code) AS country_code,
                 temperature_mean_c::text AS temperature_mean_c,
                 precipitation_mm::text AS precipitation_mm,
                 is_public_holiday,
                 holiday_name,
                 is_weekend,
                 weather_source_id,
                 holiday_source_id,
                 source_updated_at::text AS source_updated_at,
                 dataset_version,
                 weather_data_status,
                 release_id::text AS release_id
          FROM staging.daily_external_factors_ph_candidate
        ) AS candidate;
        """
    )
    return json.loads(payload[0])


def _fact_sql(release_id: str) -> str:
    quoted = sql_literal(release_id)
    return f"""
    SELECT COALESCE(json_agg(row_to_json(candidate)), '[]'::json)::text
    FROM (
      SELECT date::text AS date,
             location_id,
             btrim(country_code) AS country_code,
             temperature_mean_c::text AS temperature_mean_c,
             precipitation_mm::text AS precipitation_mm,
             is_public_holiday,
             holiday_name,
             is_weekend,
             weather_source_id,
             holiday_source_id,
             source_updated_at::text AS source_updated_at,
             dataset_version,
             weather_data_status,
             NULL::text AS release_id
      FROM publish.daily_external_factors_ph
      WHERE release_id = {quoted}::uuid
    ) AS candidate;
    """


def _snapshot_sql() -> str:
    return """
    SELECT COALESCE(json_agg(row_to_json(snapshot)), '[]'::json)::text
    FROM (
      SELECT date::text AS date, is_public_holiday, holiday_name
      FROM staging.int_holiday_calendar_snapshot
      ORDER BY date
    ) AS snapshot;
    """


def _artifact_sql() -> str:
    return """
    SELECT COALESCE(json_agg(row_to_json(artifact)), '[]'::json)::text
    FROM (
      SELECT artifact_id::text AS artifact_id,
             btrim(sha256_hex) AS sha256_hex,
             source_id
      FROM control.raw_manifests
      WHERE request_fingerprint LIKE 'stored-%'
    ) AS artifact;
    """


def _run_dbt() -> subprocess.CompletedProcess[str]:
    secrets = load_secrets("dev")
    env = os.environ.copy()
    env["OPENFACTORS_PGHOST"] = "127.0.0.1"
    env["OPENFACTORS_PGPORT"] = secrets["POSTGRES_PORT"]
    env["OPENFACTORS_PGDATABASE"] = secrets["POSTGRES_DB"]
    env["OPENFACTORS_PGPASSWORD"] = secrets["OF_TRANSFORM_PASSWORD"]
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
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(detail or "The local database query failed.")
    return [line for line in completed.stdout.splitlines() if line]


if __name__ == "__main__":
    raise SystemExit(main())
