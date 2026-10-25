"""Withdraw a source and the published releases that contain it.

There is no public cache at this cost cap. Local CSV and Parquet files for a
withdrawn release id are deleted. Raw manifests stay.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ops.local_stack import runtime_dir
from ops.migrate import psql, sql_literal
from ops.promote import DATASET_ID, PROMOTION_LOCK_KEY

PUBLIC_CACHE = "none"


@dataclass(frozen=True)
class ReleaseAccess:
    http_status: int
    release_id: str | None


@dataclass(frozen=True)
class WithdrawalResult:
    source_id: str
    withdrawn_release_ids: tuple[str, ...]
    pointer_release_id: str | None
    public_cache: str


def export_dir(environment: str, release_id: str) -> Path:
    return runtime_dir(environment) / "exports" / _uuid_text(release_id)


def suspend_source(environment: str, source_id: str) -> WithdrawalResult:
    source = _source_id(source_id)
    completed = psql(environment, "of_admin", _suspend_sql(source), tuples_only=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail)
    withdrawn = tuple(_uuid_lines(completed.stdout))
    for release_id in withdrawn:
        directory = export_dir(environment, release_id)
        if directory.exists():
            shutil.rmtree(directory)
        root = directory.parent
        if root.exists():
            for path in root.glob(f"{release_id}.*"):
                if path.is_file():
                    path.unlink()
    pointer = psql(
        environment,
        "of_admin",
        "SELECT release_id::text FROM publish.active_dataset_release "
        f"WHERE dataset_id = {sql_literal(DATASET_ID)};",
        tuples_only=True,
    )
    if pointer.returncode != 0:
        detail = (pointer.stderr or pointer.stdout).strip()
        raise RuntimeError(detail)
    current = next((line for line in pointer.stdout.splitlines() if line), None)
    return WithdrawalResult(source, withdrawn, current, PUBLIC_CACHE)


def resolve_release(environment: str, release_id: str | None) -> ReleaseAccess:
    if release_id is None:
        pointer = psql(
            environment,
            "of_admin",
            "SELECT release_id::text FROM publish.active_dataset_release "
            f"WHERE dataset_id = {sql_literal(DATASET_ID)};",
            tuples_only=True,
        )
        if pointer.returncode != 0:
            detail = (pointer.stderr or pointer.stdout).strip()
            raise RuntimeError(detail)
        current = next((line for line in pointer.stdout.splitlines() if line), None)
        if current is None:
            return ReleaseAccess(404, None)
        release_id = current
    try:
        quoted = sql_literal(_uuid_text(release_id))
    except ValueError:
        return ReleaseAccess(404, None)
    completed = psql(
        environment,
        "of_admin",
        f"""
        SELECT CASE
          WHEN releases.status = 'published'
           AND NOT EXISTS (
             SELECT 1
             FROM publish.daily_external_factors_ph AS factors
             WHERE factors.release_id = releases.release_id
               AND (
                 NOT EXISTS (
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
           )
          THEN 'found'
          ELSE 'missing'
        END
        FROM publish.dataset_releases AS releases
        WHERE releases.release_id = {quoted}::uuid;
        """,
        tuples_only=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail)
    if completed.stdout.strip() == "found":
        return ReleaseAccess(200, _uuid_text(release_id))
    return ReleaseAccess(404, None)


def _suspend_sql(source_id: str) -> str:
    quoted = sql_literal(source_id)
    dataset = sql_literal(DATASET_ID)
    return f"""
    BEGIN;
    SET LOCAL lock_timeout = '5s';
    SELECT pg_advisory_xact_lock({PROMOTION_LOCK_KEY});
    CREATE TEMP TABLE of_withdrawn (release_id uuid PRIMARY KEY) ON COMMIT PRESERVE ROWS;
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM control.source_registry WHERE source_id = {quoted}
      ) THEN
        RAISE EXCEPTION 'unknown source';
      END IF;
    END $$;
    UPDATE control.source_registry
    SET redistribution_status = 'suspended', updated_at = now()
    WHERE source_id = {quoted}
      AND redistribution_status <> 'suspended';
    INSERT INTO of_withdrawn (release_id)
    SELECT DISTINCT factors.release_id
    FROM publish.daily_external_factors_ph AS factors
    JOIN publish.dataset_releases AS releases
      ON releases.release_id = factors.release_id
    WHERE releases.status = 'published'
      AND (
        factors.weather_source_id = {quoted}
        OR factors.holiday_source_id = {quoted}
      );
    UPDATE publish.dataset_releases AS releases
    SET status = 'withdrawn'
    WHERE releases.status = 'published'
      AND releases.release_id IN (SELECT release_id FROM of_withdrawn);
    DO $$
    DECLARE
      current_id UUID;
      replacement_id UUID;
    BEGIN
      SELECT release_id INTO current_id
      FROM publish.active_dataset_release
      WHERE dataset_id = {dataset};
      IF current_id IS NULL THEN
        RETURN;
      END IF;
      IF current_id IN (SELECT release_id FROM of_withdrawn)
         OR EXISTS (
           SELECT 1 FROM publish.dataset_releases
           WHERE release_id = current_id AND status <> 'published'
         )
         OR EXISTS (
           SELECT 1 FROM publish.daily_external_factors_ph AS factors
           WHERE factors.release_id = current_id
             AND (
               factors.weather_source_id = {quoted}
               OR factors.holiday_source_id = {quoted}
             )
         )
      THEN
        SELECT releases.release_id INTO replacement_id
        FROM publish.dataset_releases AS releases
        WHERE releases.status = 'published'
          AND releases.dataset_id = {dataset}
          AND NOT EXISTS (
            SELECT 1 FROM publish.daily_external_factors_ph AS factors
            WHERE factors.release_id = releases.release_id
              AND (
                factors.weather_source_id = {quoted}
                OR factors.holiday_source_id = {quoted}
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
          )
        ORDER BY releases.published_at DESC NULLS LAST, releases.release_id DESC
        LIMIT 1;
        IF replacement_id IS NULL THEN
          DELETE FROM publish.active_dataset_release WHERE dataset_id = {dataset};
        ELSE
          UPDATE publish.active_dataset_release
          SET release_id = replacement_id, activated_at = now()
          WHERE dataset_id = {dataset};
        END IF;
      END IF;
    END $$;
    COMMIT;
    SELECT release_id::text FROM of_withdrawn ORDER BY 1;
    """


def _source_id(source_id: str) -> str:
    if not isinstance(source_id, str) or not source_id.replace("-", "").isalnum():
        raise ValueError("Unknown source.")
    if source_id != source_id.lower() or "--" in source_id:
        raise ValueError("Unknown source.")
    return source_id


def _uuid_text(release_id: str) -> str:
    return str(uuid.UUID(release_id))


def _uuid_lines(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        try:
            found.append(str(uuid.UUID(line.strip())))
        except ValueError:
            continue
    return found
