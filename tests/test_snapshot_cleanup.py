"""Explicit cleanup of an expired or demoted generation on the test cluster."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

import pytest
from ops.local_stack import ROOT, compose_base
from ops.publish_snapshot import (
    _DROP_SQL,
    _GENERATION_LOCK_CLASS,
    _GENERATION_LOCK_OBJECT,
    CANDIDATE_DATABASE,
    EXPIRED_DATABASE,
    PREVIOUS_DATABASE,
    SERVING_DATABASE,
    SnapshotError,
    _admin,
    _cutover_catalog,
    _drop_obsolete_generation,
    _generation_lock,
    _read_cleanup_identity,
    _run,
    _set_connections,
    abort_candidate,
    cleanup_generation,
    promote_candidate,
    rollback_serving,
)
from tests.release_support import (
    ENVIRONMENT,
    discard_test_releases,
    ensure_isolated_database,
)
from tests.test_publish_snapshot import _development_release, _published_release
from tests.test_snapshot_cutover import (
    _activate_another,
    _clear_cutover_databases,
    _publish_candidate,
    _session_count,
    _sleep_on,
    _wait_for_session,
)

_PROTECTED = (
    "openfactors_serving",
    "openfactors_previous",
    "openfactors_dev",
    "openfactors_test",
    "openfactors_staging",
    "postgres",
    "template0",
    "template1",
    "openfactors_expired; DROP DATABASE postgres",
    "openfactors_%",
)


def test_cleanup_drop_statements_name_one_database() -> None:
    assert _DROP_SQL == {
        "openfactors_expired": "DROP DATABASE openfactors_expired;",
        "openfactors_candidate": "DROP DATABASE openfactors_candidate;",
    }
    for statement in _DROP_SQL.values():
        assert "CASCADE" not in statement
        assert "IF EXISTS" not in statement


@pytest.mark.parametrize("name", _PROTECTED)
def test_cleanup_refuses_a_protected_or_arbitrary_name(name: str) -> None:
    with pytest.raises(SnapshotError, match="refuses this database name"):
        cleanup_generation("test", name, str(uuid.uuid4()))


def test_cleanup_rejects_the_development_cluster() -> None:
    release_id = "66196674-ae81-4c18-a0d6-85e7e0b7c056"
    with pytest.raises(SnapshotError, match="test or staging"):
        cleanup_generation("dev", EXPIRED_DATABASE, release_id)


def test_an_open_expired_generation_is_left_in_place() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    try:
        report = cleanup_generation(
            ENVIRONMENT,
            EXPIRED_DATABASE,
            str(uuid.uuid4()),
            dry_run=True,
        )
        assert report["allowed"] is False
        assert report["state"] == "open"
        assert report["connections"] == 0
        assert "accepting connections" in str(report["reason"])
        with pytest.raises(SnapshotError, match="accepting connections"):
            cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, str(uuid.uuid4()))
        assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "open"
    finally:
        _clear_cutover_databases()


def test_a_session_blocks_cleanup_without_being_terminated() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    process = _sleep_on(EXPIRED_DATABASE)
    try:
        _wait_for_session(EXPIRED_DATABASE)
        with pytest.raises(SnapshotError, match="session"):
            cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, str(uuid.uuid4()))
        assert _session_count(EXPIRED_DATABASE) == "1"
        assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "open"
    finally:
        process.kill()
        _clear_cutover_databases()


def test_cleanup_refuses_when_the_serving_database_is_missing() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=False)
    try:
        report = cleanup_generation(
            ENVIRONMENT,
            EXPIRED_DATABASE,
            str(uuid.uuid4()),
            dry_run=True,
        )
        assert report["allowed"] is False
        assert report["serving_state"] == "absent"
        assert "serving database is missing" in str(report["reason"])
        with pytest.raises(SnapshotError, match="serving database is missing"):
            cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, str(uuid.uuid4()))
        assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "closed"
        assert _cutover_catalog(ENVIRONMENT)[SERVING_DATABASE] is None
        assert _cutover_catalog(ENVIRONMENT)[PREVIOUS_DATABASE] is None
    finally:
        _clear_cutover_databases()


def test_a_failed_inspection_closes_the_target_again() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=False)
    try:
        with pytest.raises(SnapshotError, match="does not exist"):
            _read_cleanup_identity(ENVIRONMENT, EXPIRED_DATABASE, "closed")
        assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "closed"
    finally:
        _clear_cutover_databases()


def test_a_failed_reclose_leaves_recovery_instructions() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=False)
    try:
        with pytest.raises(SnapshotError, match="ALLOW_CONNECTIONS false") as caught:
            _read_cleanup_identity(ENVIRONMENT, EXPIRED_DATABASE, "closed", fail_reclose=True)
        assert "could not be closed" in str(caught.value)
        assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "open"
    finally:
        _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=False)
        _clear_cutover_databases()


def test_the_lock_session_drops_expired_and_leaves_serving() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=False)
    try:
        with _generation_lock(ENVIRONMENT) as lock:
            assert lock.session_holds_lock()
            _drop_obsolete_generation(ENVIRONMENT, EXPIRED_DATABASE)
            assert lock.session_holds_lock()
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[EXPIRED_DATABASE] is None
        assert catalog[SERVING_DATABASE] == "open"
    finally:
        _clear_cutover_databases()


def test_a_lost_generation_lock_stops_before_the_drop() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=False)
    try:
        with _generation_lock(ENVIRONMENT) as lock:
            assert lock.session_holds_lock()
            lock.lose_session()
            with pytest.raises(SnapshotError, match="generation lock was lost"):
                _drop_obsolete_generation(ENVIRONMENT, EXPIRED_DATABASE)
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[EXPIRED_DATABASE] == "closed"
        assert catalog[SERVING_DATABASE] == "open"
    finally:
        _clear_cutover_databases()


def test_a_held_generation_lock_blocks_cleanup_and_promotion() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    holder = _hold_generation_lock()
    try:
        _wait_for_generation_lock()
        with pytest.raises(SnapshotError, match="Another generation command is running"):
            cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, str(uuid.uuid4()))
        with pytest.raises(SnapshotError, match="Another generation command is running"):
            promote_candidate(ENVIRONMENT, str(uuid.uuid4()))
        assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "open"
    finally:
        _release_test_generation_lock()
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
        _clear_cutover_databases()
    with pytest.raises(SnapshotError, match="absent"):
        cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, str(uuid.uuid4()))
    assert _generation_lock_count() == "0"


def _hold_generation_lock() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            *compose_base(ENVIRONMENT),
            "exec",
            "-T",
            "postgres",
            "psql",
            "--username",
            "of_admin",
            "--dbname",
            "postgres",
            "--command",
            "SELECT pg_try_advisory_lock("
            f"{_GENERATION_LOCK_CLASS}, {_GENERATION_LOCK_OBJECT}); SELECT pg_sleep(60);",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _generation_lock_count() -> str:
    return _admin(
        ENVIRONMENT,
        "postgres",
        "SELECT count(*)::text FROM pg_locks "
        "WHERE locktype = 'advisory' AND granted "
        f"AND classid = {_GENERATION_LOCK_CLASS} "
        f"AND objid = {_GENERATION_LOCK_OBJECT};",
    ).strip()


def _wait_for_generation_lock() -> None:
    for _attempt in range(25):
        if _generation_lock_count() != "0":
            return
        time.sleep(0.2)
    raise AssertionError("The generation lock was not acquired.")


def _release_test_generation_lock() -> None:
    _admin(
        ENVIRONMENT,
        "postgres",
        "SELECT pg_terminate_backend(pid) FROM pg_locks "
        "WHERE locktype = 'advisory' AND granted "
        f"AND classid = {_GENERATION_LOCK_CLASS} "
        f"AND objid = {_GENERATION_LOCK_OBJECT};",
    )
    for _attempt in range(25):
        if _generation_lock_count() == "0":
            return
        time.sleep(0.2)
    raise AssertionError("The generation lock was not released.")


def test_cleanup_removes_only_a_confirmed_obsolete_generation(tmp_path: Path) -> None:
    dev_before = _development_release()
    ensure_isolated_database()
    _clear_cutover_databases()
    release_b = ""
    release_c = ""
    try:
        with _published_release() as (release_a, _manifest_a):
            try:
                _publish_candidate(tmp_path, release_a)
                promote_candidate(ENVIRONMENT, release_a)
                release_b = str(uuid.uuid4())
                _activate_another(release_b)
                _publish_candidate(tmp_path, release_b)
                promote_candidate(ENVIRONMENT, release_b)
                release_c = str(uuid.uuid4())
                _activate_another(release_c)
                _publish_candidate(tmp_path, release_c)
                promote_candidate(ENVIRONMENT, release_c)
                catalog = _cutover_catalog(ENVIRONMENT)
                assert catalog[SERVING_DATABASE] == "open"
                assert catalog[PREVIOUS_DATABASE] == "closed"
                assert catalog[EXPIRED_DATABASE] == "closed"

                refused = cleanup_generation(
                    ENVIRONMENT,
                    EXPIRED_DATABASE,
                    str(uuid.uuid4()),
                    dry_run=True,
                )
                assert refused["allowed"] is False
                assert "confirmation" in str(refused["reason"])
                assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "closed"
                with pytest.raises(SnapshotError, match="confirmation"):
                    cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, str(uuid.uuid4()))
                assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "closed"

                preview = cleanup_generation(
                    ENVIRONMENT,
                    EXPIRED_DATABASE,
                    release_a,
                    dry_run=True,
                )
                assert preview["allowed"] is True
                assert preview["dropped"] is False
                assert preview["release_id"] == release_a
                assert preview["state"] == "closed"
                assert preview["connections"] == 0
                assert preview["previous_state"] == "closed"
                assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "closed"

                dropped = cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, release_a)
                assert dropped["dropped"] is True
                catalog = _cutover_catalog(ENVIRONMENT)
                assert catalog[EXPIRED_DATABASE] is None
                assert catalog[PREVIOUS_DATABASE] == "closed"
                assert catalog[SERVING_DATABASE] == "open"
                with pytest.raises(SnapshotError, match="absent"):
                    cleanup_generation(ENVIRONMENT, EXPIRED_DATABASE, release_a)

                rollback_serving(ENVIRONMENT, release_b)
                catalog = _cutover_catalog(ENVIRONMENT)
                assert catalog[CANDIDATE_DATABASE] == "open"
                assert catalog[SERVING_DATABASE] == "open"
                assert catalog[PREVIOUS_DATABASE] is None
                with pytest.raises(SnapshotError, match="not dropped"):
                    abort_candidate(ENVIRONMENT)
                preview = cleanup_generation(
                    ENVIRONMENT,
                    CANDIDATE_DATABASE,
                    release_c,
                    dry_run=True,
                )
                assert preview["allowed"] is False
                assert preview["previous_state"] == "absent"
                assert "only remaining copy" in str(preview["reason"])
                with pytest.raises(SnapshotError, match="only remaining copy"):
                    cleanup_generation(ENVIRONMENT, CANDIDATE_DATABASE, release_c)
                assert _cutover_catalog(ENVIRONMENT)[CANDIDATE_DATABASE] == "open"
                kept = _admin(
                    ENVIRONMENT,
                    CANDIDATE_DATABASE,
                    "SELECT release_id::text FROM publish.active_dataset_release;",
                )
                assert release_c in kept
                assert _cutover_catalog(ENVIRONMENT)[SERVING_DATABASE] == "open"
                accepted = _run(ENVIRONMENT, SERVING_DATABASE, "SELECT 1;", user="of_api")
                assert accepted.returncode == 0
            finally:
                _clear_cutover_databases()
                if release_c:
                    discard_test_releases(ENVIRONMENT, [release_c])
                if release_b:
                    discard_test_releases(ENVIRONMENT, [release_b])
        assert _development_release() == dev_before
    finally:
        _clear_cutover_databases()
