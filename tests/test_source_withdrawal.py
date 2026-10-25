"""OF-025 source suspension, release withdrawal, and export purge."""

from __future__ import annotations

import shutil
import uuid

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.local_stack import dev_database_ready
from ops.migrate import apply, psql
from ops.promote import promote_release
from ops.releases import persist_manifest
from ops.withdraw import export_dir, resolve_release, suspend_source
from tests.test_release_manifest import _build, _row

DATASET_ID = "daily_external_factors_ph"
SOURCE_ID = "open-meteo-era5"
ALLOWED_STATUS = {"unreviewed", "approved", "rejected", "suspended"}


def test_suspended_source_withdraws_releases_and_purges_exports() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required for the takedown drill.")
    apply("dev")
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
        persist_manifest("dev", manifest)
        promote_release("dev", manifest, [row])
        export = export_dir("dev", release_id)
        export.mkdir(parents=True)
        (export / "daily.csv").write_text("drill\n", encoding="utf-8")
        (export / "daily.parquet").write_bytes(b"drill")
        keeper = export_dir("dev", keeper_id)
        keeper.mkdir(parents=True)
        (keeper / "keep.csv").write_text("keep\n", encoding="utf-8")

        blocked_row = _row()
        blocked_row["temperature_mean_c"] = "19.0"
        blocked = _build(load_json(CONTRACT_PATH), [blocked_row], release_id=blocked_id)
        persist_manifest("dev", blocked)

        result = suspend_source("dev", SOURCE_ID)
        assert result.public_cache == "none"
        assert release_id in result.withdrawn_release_ids
        assert result.pointer_release_id != release_id
        assert _release_state(release_id) == "withdrawn|t"
        assert _served_count(release_id) == "0"
        assert resolve_release("dev", release_id).http_status == 404
        assert resolve_release("dev", str(uuid.uuid4())).http_status == 404
        active = resolve_release("dev", None)
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
            promote_release("dev", blocked, [blocked_row])
        assert _release_state(blocked_id).startswith("approved|")
    finally:
        _set_source_status(prior)
        _cleanup([release_id, blocked_id])
        shutil.rmtree(export_dir("dev", release_id), ignore_errors=True)
        shutil.rmtree(export_dir("dev", keeper_id), ignore_errors=True)


def _source_status() -> str:
    return _admin(
        "SELECT redistribution_status FROM control.source_registry "
        f"WHERE source_id = '{SOURCE_ID}';"
    )[0]


def _set_source_status(status: str) -> None:
    if status not in ALLOWED_STATUS:
        raise AssertionError(status)
    completed = psql(
        "dev",
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
    completed = psql("dev", "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]


def _cleanup(release_ids: list[str]) -> None:
    quoted = ", ".join(f"'{release_id}'::uuid" for release_id in release_ids)
    completed = psql(
        "dev",
        "of_admin",
        f"""
        BEGIN;
        ALTER TABLE publish.daily_external_factors_ph DISABLE TRIGGER published_rows_immutable;
        DELETE FROM publish.daily_external_factors_ph WHERE release_id IN ({quoted});
        DELETE FROM publish.active_dataset_release WHERE release_id IN ({quoted});
        ALTER TABLE publish.daily_external_factors_ph ENABLE TRIGGER published_rows_immutable;
        COMMIT;
        """,
    )
    assert completed.returncode == 0, completed.stderr
