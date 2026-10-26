"""OF-025 source suspension, release withdrawal, and export purge."""

from __future__ import annotations

import shutil
import uuid

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.migrate import apply, psql
from ops.promote import active_release_id, promote_release, rollback_release
from ops.releases import persist_manifest
from ops.withdraw import export_dir, resolve_release, suspend_source
from tests.release_support import (
    ENVIRONMENT,
    cleanup_steps,
    discard_test_releases,
    ensure_isolated_database,
    restore_previous_release,
)
from tests.test_release_manifest import _build, _row

DATASET_ID = "daily_external_factors_ph"
SOURCE_ID = "open-meteo-era5"
ALLOWED_STATUS = {"unreviewed", "approved", "rejected", "suspended"}


def test_suspended_source_withdraws_releases_and_purges_exports() -> None:
    ensure_isolated_database()
    apply(ENVIRONMENT)
    previous = active_release_id(ENVIRONMENT)
    prior = _source_status()
    assert prior in ALLOWED_STATUS
    release_id = str(uuid.uuid4())
    blocked_id = str(uuid.uuid4())
    keeper_id = str(uuid.uuid4())
    raw_before = _count("SELECT count(*)::text FROM control.raw_manifests;")
    try:
        if prior != "approved":
            _set_source_status("approved")
        row = _row()
        manifest = _build(load_json(CONTRACT_PATH), [row], release_id=release_id)
        assert manifest.status == "approved"
        persist_manifest(ENVIRONMENT, manifest)
        promote_release(ENVIRONMENT, manifest, [row])
        export = export_dir(ENVIRONMENT, release_id)
        export.mkdir(parents=True)
        (export / "daily.csv").write_text("drill\n", encoding="utf-8")
        (export / "daily.parquet").write_bytes(b"drill")
        keeper = export_dir(ENVIRONMENT, keeper_id)
        keeper.mkdir(parents=True)
        (keeper / "keep.csv").write_text("keep\n", encoding="utf-8")

        blocked_row = _row()
        blocked_row["temperature_mean_c"] = "19.0"
        blocked = _build(load_json(CONTRACT_PATH), [blocked_row], release_id=blocked_id)
        persist_manifest(ENVIRONMENT, blocked)

        result = suspend_source(ENVIRONMENT, SOURCE_ID)
        assert result.public_cache == "none"
        assert release_id in result.withdrawn_release_ids
        assert result.pointer_release_id != release_id
        assert _release_state(release_id) == "withdrawn|t"
        assert _served_count(release_id) == "0"
        assert resolve_release(ENVIRONMENT, release_id).http_status == 404
        assert resolve_release(ENVIRONMENT, str(uuid.uuid4())).http_status == 404
        active = resolve_release(ENVIRONMENT, None)
        if result.pointer_release_id is None:
            assert active.http_status == 404
        else:
            assert active.http_status == 200
            assert active.release_id != release_id
        assert not export.exists()
        assert (keeper / "keep.csv").is_file()
        assert _source_status() == "suspended"
        assert _count("SELECT count(*)::text FROM control.raw_manifests;") == raw_before
        assert (
            _count(
                "SELECT count(*)::text FROM publish.daily_external_factors_ph "
                f"WHERE release_id = '{release_id}';"
            )
            == "1"
        )
        with pytest.raises(RuntimeError, match="revalidation"):
            promote_release(ENVIRONMENT, blocked, [blocked_row])
        assert _release_state(blocked_id).startswith("approved|")
    finally:
        cleanup_steps(
            lambda: _set_source_status(prior),
            lambda: discard_test_releases(ENVIRONMENT, [release_id, blocked_id]),
            lambda: restore_previous_release(ENVIRONMENT, previous),
            lambda: shutil.rmtree(export_dir(ENVIRONMENT, release_id), ignore_errors=True),
            lambda: shutil.rmtree(export_dir(ENVIRONMENT, keeper_id), ignore_errors=True),
        )


def _source_status() -> str:
    return _admin(
        "SELECT redistribution_status FROM control.source_registry "
        f"WHERE source_id = '{SOURCE_ID}';"
    )[0]


def _set_source_status(status: str) -> None:
    if status not in ALLOWED_STATUS:
        raise AssertionError(status)
    completed = psql(
        ENVIRONMENT,
        "of_admin",
        "UPDATE control.source_registry "
        f"SET redistribution_status = '{status}', updated_at = now() "
        f"WHERE source_id = '{SOURCE_ID}';",
    )
    assert completed.returncode == 0, completed.stderr


def _release_state(release_id: str) -> str:
    return _admin(
        "SELECT status, published_at IS NOT NULL "
        f"FROM publish.dataset_releases WHERE release_id = '{release_id}';"
    )[0]


def _served_count(release_id: str) -> str:
    return _admin(
        "SELECT count(*)::text "
        "FROM publish.daily_external_factors_ph AS factors "
        "JOIN publish.active_dataset_release AS active "
        "  ON active.release_id = factors.release_id "
        "JOIN publish.dataset_releases AS releases "
        "  ON releases.release_id = factors.release_id "
        " AND releases.status = 'published' "
        f"WHERE factors.release_id = '{release_id}' "
        f"  AND active.dataset_id = '{DATASET_ID}';"
    )[0]


def _count(sql: str) -> str:
    return _admin(sql)[0]


def _admin(sql: str) -> list[str]:
    completed = psql(ENVIRONMENT, "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]


def test_withdrawal_does_not_activate_a_release_with_missing_rows() -> None:
    ensure_isolated_database()
    apply(ENVIRONMENT)
    previous = active_release_id(ENVIRONMENT)
    prior = _source_status()
    keeper_id = str(uuid.uuid4())
    broken_id = str(uuid.uuid4())
    try:
        if prior != "approved":
            _set_source_status("approved")
        keeper = _row()
        keeper_manifest = _build(load_json(CONTRACT_PATH), [keeper], release_id=keeper_id)
        assert keeper_manifest.status == "approved"
        persist_manifest(ENVIRONMENT, keeper_manifest)
        promote_release(ENVIRONMENT, keeper_manifest, [keeper])
        broken = _row()
        broken["temperature_mean_c"] = "19.4"
        broken_manifest = _build(load_json(CONTRACT_PATH), [broken], release_id=broken_id)
        assert broken_manifest.status == "approved"
        persist_manifest(ENVIRONMENT, broken_manifest)
        promote_release(ENVIRONMENT, broken_manifest, [broken])
        _delete_fact_rows(broken_id)
        assert _release_state(broken_id) == "published|t"
        assert _stored_count(broken_id) == "0"
        rollback_release(ENVIRONMENT, keeper_id)
        result = suspend_source(ENVIRONMENT, SOURCE_ID)
        assert keeper_id in result.withdrawn_release_ids
        assert broken_id not in result.withdrawn_release_ids
        assert result.pointer_release_id != broken_id
        assert active_release_id(ENVIRONMENT) != broken_id
        pointer = active_release_id(ENVIRONMENT)
        if pointer is not None:
            assert _stored_count(pointer) == _declared_count(pointer)
        with pytest.raises(RuntimeError, match="content count mismatch"):
            rollback_release(ENVIRONMENT, broken_id)
    finally:
        cleanup_steps(
            lambda: _set_source_status(prior),
            lambda: discard_test_releases(ENVIRONMENT, [keeper_id, broken_id]),
            lambda: restore_previous_release(ENVIRONMENT, previous),
        )


def _delete_fact_rows(release_id: str) -> None:
    quoted = f"'{uuid.UUID(release_id)}'::uuid"
    completed = psql(
        ENVIRONMENT,
        "of_admin",
        f"""
        BEGIN;
        ALTER TABLE publish.daily_external_factors_ph DISABLE TRIGGER published_rows_immutable;
        DELETE FROM publish.daily_external_factors_ph WHERE release_id = {quoted};
        ALTER TABLE publish.daily_external_factors_ph ENABLE TRIGGER published_rows_immutable;
        COMMIT;
        """,
    )
    assert completed.returncode == 0, completed.stderr


def _stored_count(release_id: str) -> str:
    quoted = f"'{uuid.UUID(release_id)}'::uuid"
    return _admin(
        f"SELECT count(*)::text FROM publish.daily_external_factors_ph WHERE release_id = {quoted};"
    )[0]


def _declared_count(release_id: str) -> str:
    quoted = f"'{uuid.UUID(release_id)}'::uuid"
    return _admin(
        f"SELECT actual_row_count::text FROM publish.dataset_releases WHERE release_id = {quoted};"
    )[0]
