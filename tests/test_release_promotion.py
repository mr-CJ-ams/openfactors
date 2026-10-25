"""OF-024 atomic pointer switch, failed promotion, and rollback."""

from __future__ import annotations

import subprocess
import time
import uuid
from decimal import Decimal

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.local_stack import compose_base, docker_available, load_secrets
from ops.migrate import apply, psql
from ops.promote import PROMOTION_LOCK_KEY, promote_release, rollback_release
from ops.releases import persist_manifest
from tests.test_release_manifest import _build, _row

DATASET_ID = "daily_external_factors_ph"


def test_pointer_switch_failure_and_rollback_keep_a_whole_release() -> None:
    if not docker_available():
        pytest.skip("Docker is required to promote a release.")
    apply("dev")
    _terminate_lock_holder()
    contract = load_json(CONTRACT_PATH)
    release_a = str(uuid.uuid4())
    release_b = str(uuid.uuid4())
    release_crash = str(uuid.uuid4())
    release_rejected = str(uuid.uuid4())
    created = [release_a, release_b, release_crash, release_rejected]
    try:
        first = _approved(contract, _row(), release_a)
        persist_manifest("dev", first)
        promote_release("dev", first, [_row()])
        assert _active() == release_a
        assert _served_temperature() == Decimal("26.1")
        assert _stored_count(release_a) == "1"

        second_row = _row()
        second_row["temperature_mean_c"] = "18.4"
        second = _approved(contract, second_row, release_b)
        persist_manifest("dev", second)
        with _held_promotion_lock(), pytest.raises(RuntimeError, match="lock timeout"):
            promote_release("dev", second, [second_row], lock_timeout_s=2)
        assert _active() == release_a
        assert _stored_count(release_b) == "0"
        assert _served_temperature() == Decimal("26.1")

        promote_release("dev", second, [second_row])
        assert _active() == release_b
        assert _served_temperature() == Decimal("18.4")
        assert _stored_count(release_a) == "1"
        assert _stored_count(release_b) == "1"
        assert _view_count() == "1"

        rollback_release("dev", release_a)
        assert _active() == release_a
        assert _served_temperature() == Decimal("26.1")
        assert _stored_count(release_b) == "1"
        assert _status(release_b) == "published"

        rejected_row = _row()
        rejected_row["precipitation_mm"] = "-1"
        rejected = _build(contract, [rejected_row], release_id=release_rejected)
        assert rejected.status == "rejected"
        persist_manifest("dev", rejected)
        with pytest.raises(ValueError, match="not promoted"):
            promote_release("dev", rejected, [rejected_row])
        denied_pointer = psql(
            "dev",
            "of_admin",
            "UPDATE publish.active_dataset_release "
            f"SET release_id = '{release_rejected}' WHERE dataset_id = '{DATASET_ID}';",
        )
        assert denied_pointer.returncode != 0
        assert "published" in denied_pointer.stderr
        assert _active() == release_a
        assert _served_temperature() == Decimal("26.1")

        crash_row = _row()
        crash_row["temperature_mean_c"] = "21.0"
        crashed = _approved(contract, crash_row, release_crash)
        persist_manifest("dev", crashed)
        with pytest.raises(RuntimeError, match="injected promotion failure"):
            promote_release("dev", crashed, [crash_row], abort_before_commit=True)
        assert _status(release_crash) == "approved"
        assert _stored_count(release_crash) == "0"
        assert _active() == release_a
        assert _served_temperature() == Decimal("26.1")

        for statement in (
            "UPDATE publish.daily_external_factors_ph SET temperature_mean_c = 0 "
            f"WHERE release_id = '{release_a}';",
            f"DELETE FROM publish.daily_external_factors_ph WHERE release_id = '{release_a}';",
            "UPDATE publish.dataset_releases SET content_sha256_hex = repeat('b', 64) "
            f"WHERE release_id = '{release_a}';",
            "UPDATE publish.dataset_releases SET status = 'approved', published_at = NULL "
            f"WHERE release_id = '{release_a}';",
        ):
            denied = psql("dev", "of_admin", statement)
            assert denied.returncode != 0, statement
            assert "immutable" in denied.stderr
    finally:
        _terminate_lock_holder()
        _cleanup(created)


def _approved(contract: dict, row: dict, release_id: str):
    manifest = _build(contract, [row], release_id=release_id)
    assert manifest.status == "approved"
    assert manifest.schema_version == "1.0.0"
    assert manifest.release_id != manifest.schema_version
    return manifest


def _active() -> str:
    return _admin(
        "SELECT release_id::text FROM publish.active_dataset_release "
        f"WHERE dataset_id = '{DATASET_ID}';"
    )[0]


def _served_temperature() -> Decimal:
    served = psql(
        "dev",
        "of_api",
        "SELECT location_id, temperature_mean_c::text, dataset_version FROM publish.daily_factors;",
        tuples_only=True,
    )
    assert served.returncode == 0, served.stderr
    rows = [line for line in served.stdout.splitlines() if line]
    assert len(rows) == 1
    location_id, temperature, version = rows[0].split("|")
    assert location_id == "ph-manila"
    assert version == "1.0.0"
    return Decimal(temperature)


def _stored_count(release_id: str) -> str:
    rows = _admin(
        "SELECT count(*)::text FROM publish.daily_external_factors_ph "
        f"WHERE release_id = '{release_id}';"
    )
    return rows[0]


def _view_count() -> str:
    counted = psql(
        "dev",
        "of_api",
        "SELECT count(*)::text FROM publish.daily_factors;",
        tuples_only=True,
    )
    assert counted.returncode == 0, counted.stderr
    return counted.stdout.strip()


def _status(release_id: str) -> str:
    return _admin(
        f"SELECT status FROM publish.dataset_releases WHERE release_id = '{release_id}';"
    )[0]


def _admin(sql: str) -> list[str]:
    completed = psql("dev", "of_admin", sql, tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    return [line for line in completed.stdout.splitlines() if line]


def _cleanup(release_ids: list[str]) -> None:
    if not release_ids:
        return
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


def _terminate_lock_holder() -> None:
    psql(
        "dev",
        "of_admin",
        """
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE application_name = 'of024-lock-holder'
          AND pid <> pg_backend_pid();
        """,
    )


class _held_promotion_lock:
    def __enter__(self):
        secrets = load_secrets("dev")
        self._secret = secrets["POSTGRES_PASSWORD"]
        database = "openfactors_dev"
        command = [
            *compose_base("dev"),
            "exec",
            "-T",
            "--env",
            f"PGPASSWORD={self._secret}",
            "postgres",
            "psql",
            f"host=127.0.0.1 user=of_admin dbname={database} sslmode=require",
            "--no-password",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self._process.stdin is not None
        self._process.stdin.write(
            f"""
            SET application_name = 'of024-lock-holder';
            SELECT pg_advisory_lock({PROMOTION_LOCK_KEY});
            SELECT pg_sleep(20);
            """
        )
        self._process.stdin.close()
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                if self._process.poll() is not None:
                    detail = self._stderr()
                    raise AssertionError(f"Lock holder exited early.\n{detail}")
                locked = _admin(
                    """
                    SELECT count(*)::text
                    FROM pg_locks AS locks
                    JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
                    WHERE locks.locktype = 'advisory'
                      AND locks.granted
                      AND activity.application_name = 'of024-lock-holder';
                    """
                )
                if locked == ["1"]:
                    return self
                time.sleep(0.2)
            raise AssertionError("Promotion lock was not acquired.")
        except Exception:
            self._stop()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._stop()
        return False

    def _stop(self) -> None:
        _terminate_lock_holder()
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait(timeout=15)

    def _stderr(self) -> str:
        if self._process.stderr is None:
            return ""
        detail = self._process.stderr.read() or ""
        return detail.replace(self._secret, "***")
