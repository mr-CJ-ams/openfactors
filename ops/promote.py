"""Promote or roll back the active release in one transaction.

A rejected candidate is not inserted. A statement that fails before commit
leaves the previous pointer and the previous rows in place.
"""

from __future__ import annotations

from openfactors.quality.manifest import ReleaseManifest, canonical_rows, content_sha256
from ops.migrate import psql, sql_literal

PROMOTION_LOCK_KEY = 824024
DATASET_ID = "daily_external_factors_ph"


def promote_release(
    environment: str,
    manifest: ReleaseManifest,
    rows: list[dict],
    *,
    lock_timeout_s: int = 5,
    abort_before_commit: bool = False,
) -> None:
    if manifest.status != "approved":
        raise ValueError("A rejected candidate is not promoted.")
    if content_sha256(rows) != manifest.content_sha256_hex:
        raise ValueError("Content checksum does not match the release manifest.")
    if len(rows) != manifest.actual_row_count:
        raise ValueError("Content count does not match the release manifest.")
    normalized = canonical_rows(rows)
    _run(
        environment,
        _promote_sql(
            manifest,
            normalized,
            lock_timeout_s=_timeout(lock_timeout_s),
            abort_before_commit=abort_before_commit,
        ),
    )


def rollback_release(environment: str, release_id: str, *, lock_timeout_s: int = 5) -> None:
    _run(environment, _rollback_sql(release_id, lock_timeout_s=_timeout(lock_timeout_s)))


def active_release_id(environment: str) -> str | None:
    lines = _text_lines(
        environment,
        "SELECT release_id::text FROM publish.active_dataset_release "
        f"WHERE dataset_id = {sql_literal(DATASET_ID)};",
    )
    return lines[0] if lines else None


def restore_active_release(environment: str, previous: str | None) -> None:
    """Point the local view back at the release that was active before a drill."""
    if previous is None:
        return
    status = _release_status(environment, previous)
    if status == "published":
        if active_release_id(environment) != previous:
            rollback_release(environment, previous)
        return
    if status == "withdrawn":
        from ops.load_stored import restore_withdrawn_release

        restore_withdrawn_release(previous)


def _release_status(environment: str, release_id: str) -> str | None:
    lines = _text_lines(
        environment,
        "SELECT status FROM publish.dataset_releases "
        f"WHERE release_id = {sql_literal(release_id)}::uuid;",
    )
    return lines[0] if lines else None


def _text_lines(environment: str, sql: str) -> list[str]:
    completed = psql(environment, "of_admin", sql, tuples_only=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or "The local database query failed.")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _promote_sql(
    manifest: ReleaseManifest,
    rows: list[dict],
    *,
    lock_timeout_s: int,
    abort_before_commit: bool,
) -> str:
    release_id = sql_literal(manifest.release_id)
    values = ",\n".join(f"({_row_sql(manifest.release_id, row)})" for row in rows)
    abort = (
        "DO $$ BEGIN RAISE EXCEPTION 'injected promotion failure'; END $$;\n"
        if abort_before_commit
        else ""
    )
    return f"""
    BEGIN;
    SET LOCAL lock_timeout = '{lock_timeout_s}s';
    SELECT pg_advisory_xact_lock({PROMOTION_LOCK_KEY});
    DO $$
    DECLARE
      found_status TEXT;
      found_hash TEXT;
      found_count BIGINT;
      found_version TEXT;
      pass_count INTEGER;
    BEGIN
      SELECT status, btrim(content_sha256_hex), actual_row_count, schema_version
        INTO found_status, found_hash, found_count, found_version
      FROM publish.dataset_releases
      WHERE release_id = {release_id}::uuid
      FOR UPDATE;
      IF found_status IS DISTINCT FROM 'approved' THEN
        RAISE EXCEPTION 'release is not an approved candidate';
      END IF;
      IF found_hash IS DISTINCT FROM {sql_literal(manifest.content_sha256_hex)} THEN
        RAISE EXCEPTION 'content checksum mismatch';
      END IF;
      IF found_count IS DISTINCT FROM {manifest.actual_row_count} THEN
        RAISE EXCEPTION 'content count mismatch';
      END IF;
      IF found_version IS DISTINCT FROM {sql_literal(manifest.schema_version)} THEN
        RAISE EXCEPTION 'schema version mismatch';
      END IF;
      SELECT count(*) INTO pass_count
      FROM quality.validation_runs
      WHERE release_id = {release_id}::uuid AND status = 'PASS';
      IF pass_count < 1 THEN
        RAISE EXCEPTION 'quality evidence is not PASS';
      END IF;
    END $$;
    INSERT INTO publish.daily_external_factors_ph (
      release_id, date, location_id, country_code,
      temperature_mean_c, precipitation_mm,
      is_public_holiday, holiday_name, is_weekend,
      weather_source_id, holiday_source_id, source_updated_at,
      dataset_version, weather_data_status
    ) VALUES
    {values};
    DO $$
    DECLARE
      stored_count BIGINT;
    BEGIN
      SELECT count(*) INTO stored_count
      FROM publish.daily_external_factors_ph
      WHERE release_id = {release_id}::uuid;
      IF stored_count IS DISTINCT FROM {manifest.actual_row_count} THEN
        RAISE EXCEPTION 'content count mismatch';
      END IF;
      IF EXISTS (
        SELECT 1
        FROM publish.daily_external_factors_ph AS factors
        WHERE factors.release_id = {release_id}::uuid
          AND (
            factors.dataset_version IS DISTINCT FROM {sql_literal(manifest.schema_version)}
            OR NOT EXISTS (
              SELECT 1 FROM control.source_registry AS registry
              WHERE registry.source_id = factors.weather_source_id
                AND registry.redistribution_status = 'approved'
            )
            OR NOT EXISTS (
              SELECT 1 FROM control.source_registry AS registry
              WHERE registry.source_id = factors.holiday_source_id
                AND registry.redistribution_status = 'approved'
            )
          )
      ) THEN
        RAISE EXCEPTION 'release rows failed revalidation';
      END IF;
    END $$;
    DO $$
    DECLARE
      updated INTEGER;
    BEGIN
      UPDATE publish.dataset_releases
      SET status = 'published', published_at = now()
      WHERE release_id = {release_id}::uuid AND status = 'approved';
      GET DIAGNOSTICS updated = ROW_COUNT;
      IF updated <> 1 THEN
        RAISE EXCEPTION 'release is not an approved candidate';
      END IF;
    END $$;
    {abort}INSERT INTO publish.active_dataset_release (dataset_id, release_id, activated_at)
    VALUES ({sql_literal(DATASET_ID)}, {release_id}::uuid, now())
    ON CONFLICT (dataset_id) DO UPDATE
    SET release_id = EXCLUDED.release_id, activated_at = now();
    COMMIT;
    """


def _rollback_sql(release_id: str, *, lock_timeout_s: int) -> str:
    quoted = sql_literal(str(release_id))
    return f"""
    BEGIN;
    SET LOCAL lock_timeout = '{lock_timeout_s}s';
    SELECT pg_advisory_xact_lock({PROMOTION_LOCK_KEY});
    DO $$
    DECLARE
      found_status TEXT;
      found_count BIGINT;
      found_version TEXT;
      stored_count BIGINT;
      pass_count INTEGER;
    BEGIN
      SELECT status, actual_row_count, schema_version
        INTO found_status, found_count, found_version
      FROM publish.dataset_releases
      WHERE release_id = {quoted}::uuid
      FOR UPDATE;
      IF found_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'release is not published';
      END IF;
      SELECT count(*) INTO pass_count
      FROM quality.validation_runs
      WHERE release_id = {quoted}::uuid AND status = 'PASS';
      IF pass_count < 1 THEN
        RAISE EXCEPTION 'quality evidence is not PASS';
      END IF;
      SELECT count(*) INTO stored_count
      FROM publish.daily_external_factors_ph
      WHERE release_id = {quoted}::uuid;
      IF stored_count IS DISTINCT FROM found_count THEN
        RAISE EXCEPTION 'content count mismatch';
      END IF;
      IF EXISTS (
        SELECT 1
        FROM publish.daily_external_factors_ph AS factors
        WHERE factors.release_id = {quoted}::uuid
          AND (
            factors.dataset_version IS DISTINCT FROM found_version
            OR NOT EXISTS (
              SELECT 1 FROM control.source_registry AS registry
              WHERE registry.source_id = factors.weather_source_id
                AND registry.redistribution_status = 'approved'
            )
            OR NOT EXISTS (
              SELECT 1 FROM control.source_registry AS registry
              WHERE registry.source_id = factors.holiday_source_id
                AND registry.redistribution_status = 'approved'
            )
          )
      ) THEN
        RAISE EXCEPTION 'release rows failed revalidation';
      END IF;
    END $$;
    INSERT INTO publish.active_dataset_release (dataset_id, release_id, activated_at)
    VALUES ({sql_literal(DATASET_ID)}, {quoted}::uuid, now())
    ON CONFLICT (dataset_id) DO UPDATE
    SET release_id = EXCLUDED.release_id, activated_at = now();
    COMMIT;
    """


def _row_sql(release_id: str, row: dict) -> str:
    return ", ".join(
        (
            f"{sql_literal(release_id)}::uuid",
            f"{_required(row['date'])}::date",
            _required(row["location_id"]),
            _required(row["country_code"]),
            _numeric(row["temperature_mean_c"]),
            _numeric(row["precipitation_mm"]),
            _boolean(row["is_public_holiday"]),
            _optional(row["holiday_name"]),
            _boolean(row["is_weekend"]),
            _required(row["weather_source_id"]),
            _required(row["holiday_source_id"]),
            _optional_timestamp(row["source_updated_at"]),
            _required(row["dataset_version"]),
            _required(row["weather_data_status"]),
        )
    )


def _required(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A promoted row is missing a required value.")
    return sql_literal(value)


def _optional(value: object) -> str:
    if value is None:
        return "NULL"
    if not isinstance(value, str):
        raise ValueError("A promoted text value is not text.")
    return sql_literal(value)


def _optional_timestamp(value: object) -> str:
    if value is None:
        return "NULL"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A promoted timestamp is not text.")
    return f"{sql_literal(value)}::timestamptz"


def _numeric(value: object) -> str:
    if value is None:
        return "NULL"
    if not isinstance(value, str):
        raise ValueError("A promoted measure is not text.")
    return f"{sql_literal(value)}::numeric"


def _boolean(value: object) -> str:
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    raise ValueError("A promoted flag is not a boolean.")


def _timeout(lock_timeout_s: int) -> int:
    if isinstance(lock_timeout_s, bool) or not isinstance(lock_timeout_s, int):
        raise ValueError("Lock timeout must be a whole number of seconds.")
    if not 1 <= lock_timeout_s <= 60:
        raise ValueError("Lock timeout must be from 1 to 60 seconds.")
    return lock_timeout_s


def _run(environment: str, statement: str) -> None:
    completed = psql(environment, "of_admin", statement)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail)
