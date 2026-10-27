"""Cutover and rollback on the isolated test cluster."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.local_stack import ROOT, compose_base
from ops.promote import promote_release
from ops.publish_snapshot import (
    CANDIDATE_DATABASE,
    EXPIRED_DATABASE,
    PREVIOUS_DATABASE,
    SERVING_DATABASE,
    SnapshotError,
    _admin,
    _cutover_catalog,
    _database_comment,
    _promotion_plan,
    _rename_transaction,
    _run,
    _set_connections,
    _terminate_database,
    _undo_promotion,
    abort_candidate,
    create_snapshot,
    promote_candidate,
    restore_candidate,
    rollback_serving,
)
from ops.releases import persist_manifest
from tests.release_support import (
    ENVIRONMENT,
    discard_test_releases,
    ensure_isolated_database,
)
from tests.test_publish_snapshot import _development_release, _published_release
from tests.test_release_manifest import _row
from tests.test_release_promotion import _approved

_ROLES = """
SELECT rolname || '|' || CASE WHEN rolcanlogin THEN 'login' ELSE 'nologin' END
FROM pg_roles
WHERE rolname IN ('of_admin', 'of_migrate', 'of_ingest', 'of_transform', 'of_publish', 'of_api')
ORDER BY 1;
"""


def test_promotion_plans_reject_an_unexpected_catalog() -> None:
    open_candidate = {
        CANDIDATE_DATABASE: "open",
        SERVING_DATABASE: None,
        PREVIOUS_DATABASE: None,
        EXPIRED_DATABASE: None,
    }
    assert _promotion_plan(open_candidate) == [(CANDIDATE_DATABASE, SERVING_DATABASE)]
    later = {
        CANDIDATE_DATABASE: "open",
        SERVING_DATABASE: "open",
        PREVIOUS_DATABASE: "closed",
        EXPIRED_DATABASE: None,
    }
    assert _promotion_plan(later) == [
        (PREVIOUS_DATABASE, EXPIRED_DATABASE),
        (SERVING_DATABASE, PREVIOUS_DATABASE),
        (CANDIDATE_DATABASE, SERVING_DATABASE),
    ]
    blocked = dict(later)
    blocked[EXPIRED_DATABASE] = "closed"
    with pytest.raises(SnapshotError, match="expired"):
        _promotion_plan(blocked)
    unexpected = dict(open_candidate)
    unexpected[PREVIOUS_DATABASE] = "closed"
    with pytest.raises(SnapshotError, match="unexpected"):
        _promotion_plan(unexpected)


def test_cutover_rejects_the_development_cluster() -> None:
    release_id = "66196674-ae81-4c18-a0d6-85e7e0b7c056"
    with pytest.raises(SnapshotError, match="test or staging"):
        promote_candidate("dev", release_id)
    with pytest.raises(SnapshotError, match="test or staging"):
        rollback_serving("dev", release_id)


def test_a_blocked_rename_keeps_the_original_names() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    process = _sleep_on(SERVING_DATABASE)
    try:
        _wait_for_session(SERVING_DATABASE)
        with pytest.raises(SnapshotError):
            _rename_transaction(ENVIRONMENT, [(SERVING_DATABASE, PREVIOUS_DATABASE)])
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[SERVING_DATABASE] == "open"
        assert catalog[PREVIOUS_DATABASE] is None
    finally:
        process.kill()
        _clear_cutover_databases()


def test_closing_the_serving_database_blocks_new_sessions() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    process = _sleep_on(SERVING_DATABASE)
    try:
        _wait_for_session(SERVING_DATABASE)
        _set_connections(ENVIRONMENT, SERVING_DATABASE, allow=False)
        refused = _run(ENVIRONMENT, SERVING_DATABASE, "SELECT 1;", user="of_api")
        assert refused.returncode != 0
        assert "not currently accepting connections" in (refused.stderr or "")
        assert _session_count(SERVING_DATABASE) == "1"
        assert _cutover_catalog(ENVIRONMENT)[SERVING_DATABASE] == "closed"
        _terminate_database(ENVIRONMENT, SERVING_DATABASE)
        assert _session_count(SERVING_DATABASE) == "0"
    finally:
        process.kill()
        _clear_cutover_databases()


def test_a_compensating_transaction_failure_before_commit_keeps_whole_generations() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {PREVIOUS_DATABASE};")
    before = _generation_oids()
    try:
        with pytest.raises(SnapshotError, match="failed before commit and was rolled back"):
            _undo_promotion(
                ENVIRONMENT,
                [
                    (SERVING_DATABASE, PREVIOUS_DATABASE),
                    (CANDIDATE_DATABASE, SERVING_DATABASE),
                ],
                SnapshotError("Post-cutover validation failed."),
                fail_before_commit=True,
            )
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[SERVING_DATABASE] == "open"
        assert catalog[PREVIOUS_DATABASE] == "open"
        assert catalog[CANDIDATE_DATABASE] is None
        assert catalog[EXPIRED_DATABASE] is None
        assert _generation_oids() == before
    finally:
        _clear_cutover_databases()


def test_a_rollback_mark_rolls_back_with_the_rename() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {PREVIOUS_DATABASE};")
    before = _generation_oids()
    mark = f"COMMENT ON DATABASE {CANDIDATE_DATABASE} IS 'openfactors-demoted';"
    try:
        with pytest.raises(SnapshotError):
            _rename_transaction(
                ENVIRONMENT,
                [
                    (SERVING_DATABASE, CANDIDATE_DATABASE),
                    (PREVIOUS_DATABASE, SERVING_DATABASE),
                ],
                after=(mark,),
                fail_before_commit=True,
            )
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[SERVING_DATABASE] == "open"
        assert catalog[PREVIOUS_DATABASE] == "open"
        assert catalog[CANDIDATE_DATABASE] is None
        assert catalog[EXPIRED_DATABASE] is None
        assert _generation_oids() == before
        assert _database_comment(ENVIRONMENT, SERVING_DATABASE) == ""
        assert _database_comment(ENVIRONMENT, PREVIOUS_DATABASE) == ""
    finally:
        _clear_cutover_databases()


def test_a_committed_close_survives_a_failed_rename() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    process = _sleep_on(SERVING_DATABASE)
    try:
        _wait_for_session(SERVING_DATABASE)
        _set_connections(ENVIRONMENT, SERVING_DATABASE, allow=False)
        with pytest.raises(SnapshotError):
            _rename_transaction(ENVIRONMENT, [(SERVING_DATABASE, PREVIOUS_DATABASE)])
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[SERVING_DATABASE] == "closed"
        assert catalog[PREVIOUS_DATABASE] is None
        refused = _run(ENVIRONMENT, SERVING_DATABASE, "SELECT 1;", user="of_api")
        assert refused.returncode != 0
        assert "not currently accepting connections" in (refused.stderr or "")
        assert _session_count(SERVING_DATABASE) == "1"
    finally:
        process.kill()
        _clear_cutover_databases()


def test_an_open_previous_generation_is_left_unchanged() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    for name in (CANDIDATE_DATABASE, SERVING_DATABASE, PREVIOUS_DATABASE):
        _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {name};")
    try:
        with pytest.raises(SnapshotError, match="accepting connections"):
            promote_candidate(ENVIRONMENT, str(uuid.uuid4()))
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[CANDIDATE_DATABASE] == "open"
        assert catalog[SERVING_DATABASE] == "open"
        assert catalog[PREVIOUS_DATABASE] == "open"
        assert catalog[EXPIRED_DATABASE] is None
    finally:
        _clear_cutover_databases()


def test_a_demoted_candidate_is_not_promoted_or_dropped() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {CANDIDATE_DATABASE};")
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {SERVING_DATABASE};")
    _admin(
        ENVIRONMENT,
        "postgres",
        f"COMMENT ON DATABASE {CANDIDATE_DATABASE} IS 'openfactors-demoted';",
    )
    try:
        with pytest.raises(SnapshotError, match="preserved serving generation"):
            promote_candidate(ENVIRONMENT, str(uuid.uuid4()))
        with pytest.raises(SnapshotError, match="not dropped"):
            abort_candidate(ENVIRONMENT)
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[CANDIDATE_DATABASE] == "open"
        assert catalog[SERVING_DATABASE] == "open"
        assert catalog[PREVIOUS_DATABASE] is None
        assert catalog[EXPIRED_DATABASE] is None
    finally:
        _clear_cutover_databases()


def test_an_expired_generation_blocks_promotion() -> None:
    ensure_isolated_database()
    _clear_cutover_databases()
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {CANDIDATE_DATABASE};")
    _admin(ENVIRONMENT, "postgres", f"CREATE DATABASE {EXPIRED_DATABASE};")
    try:
        with pytest.raises(SnapshotError, match="expired"):
            promote_candidate(ENVIRONMENT, str(uuid.uuid4()))
        catalog = _cutover_catalog(ENVIRONMENT)
        assert catalog[CANDIDATE_DATABASE] == "open"
        assert catalog[EXPIRED_DATABASE] == "open"
        assert catalog[SERVING_DATABASE] is None
    finally:
        _clear_cutover_databases()


def test_promotions_and_rollback_preserve_generations_and_development_data(tmp_path: Path) -> None:
    dev_before = _development_release()
    ensure_isolated_database()
    _clear_cutover_databases()
    roles_before = _admin(ENVIRONMENT, "postgres", _ROLES)
    release_b = ""
    release_c = ""
    try:
        with _published_release() as (release_a, manifest_a):
            try:
                _publish_candidate(tmp_path, release_a)
                with pytest.raises(SnapshotError, match="confirmation"):
                    promote_candidate(ENVIRONMENT, str(uuid.uuid4()))
                with pytest.raises(SnapshotError, match="Post-cutover validation failed"):
                    promote_candidate(ENVIRONMENT, release_a, abort_after_rename=True)
                assert _cutover_catalog(ENVIRONMENT)[CANDIDATE_DATABASE] == "open"
                assert _cutover_catalog(ENVIRONMENT)[SERVING_DATABASE] is None
                first = promote_candidate(ENVIRONMENT, release_a)
                assert first["database"] == SERVING_DATABASE
                assert first["release_id"] == release_a
                assert first["content_sha256_hex"] == manifest_a.content_sha256_hex
                assert _cutover_catalog(ENVIRONMENT)[PREVIOUS_DATABASE] is None
                with pytest.raises(SnapshotError, match="no previous"):
                    rollback_serving(ENVIRONMENT, release_a)

                release_b = str(uuid.uuid4())
                manifest_b = _activate_another(release_b)
                _publish_candidate(tmp_path, release_b)
                with pytest.raises(SnapshotError, match="Post-cutover validation failed"):
                    promote_candidate(ENVIRONMENT, release_b, abort_after_rename=True)
                catalog = _cutover_catalog(ENVIRONMENT)
                assert catalog[SERVING_DATABASE] == "open"
                assert catalog[CANDIDATE_DATABASE] == "open"
                assert catalog[PREVIOUS_DATABASE] is None
                assert catalog[EXPIRED_DATABASE] is None
                restored_serving = _admin(
                    ENVIRONMENT,
                    SERVING_DATABASE,
                    "SELECT release_id::text FROM publish.active_dataset_release;",
                )
                restored_candidate = _admin(
                    ENVIRONMENT,
                    CANDIDATE_DATABASE,
                    "SELECT release_id::text FROM publish.active_dataset_release;",
                )
                assert release_a in restored_serving
                assert release_b in restored_candidate
                still_serving = _run(ENVIRONMENT, SERVING_DATABASE, "SELECT 1;", user="of_api")
                assert still_serving.returncode == 0

                process = _sleep_on(SERVING_DATABASE)
                try:
                    _wait_for_session(SERVING_DATABASE)
                    _set_connections(ENVIRONMENT, SERVING_DATABASE, allow=False)
                    refused = _run(ENVIRONMENT, SERVING_DATABASE, "SELECT 1;", user="of_api")
                    assert refused.returncode != 0
                    assert "not currently accepting connections" in (refused.stderr or "")
                    with pytest.raises(SnapshotError):
                        _rename_transaction(
                            ENVIRONMENT,
                            [
                                (SERVING_DATABASE, PREVIOUS_DATABASE),
                                (CANDIDATE_DATABASE, SERVING_DATABASE),
                            ],
                        )
                    catalog = _cutover_catalog(ENVIRONMENT)
                    assert catalog[SERVING_DATABASE] == "closed"
                    assert catalog[CANDIDATE_DATABASE] == "open"
                    assert catalog[PREVIOUS_DATABASE] is None
                    assert catalog[EXPIRED_DATABASE] is None
                finally:
                    process.kill()
                    _terminate_database(ENVIRONMENT, SERVING_DATABASE)
                second = promote_candidate(ENVIRONMENT, release_b)
                assert second["release_id"] == release_b
                assert second["content_sha256_hex"] == manifest_b.content_sha256_hex
                catalog = _cutover_catalog(ENVIRONMENT)
                assert catalog[SERVING_DATABASE] == "open"
                assert catalog[PREVIOUS_DATABASE] == "closed"
                assert catalog[EXPIRED_DATABASE] is None
                retry = promote_candidate(ENVIRONMENT, release_b)
                assert retry["release_id"] == release_b
                assert _cutover_catalog(ENVIRONMENT)[PREVIOUS_DATABASE] == "closed"
                accepted = _run(ENVIRONMENT, SERVING_DATABASE, "SELECT 1;", user="of_api")
                assert accepted.returncode == 0
                still_closed = _run(ENVIRONMENT, PREVIOUS_DATABASE, "SELECT 1;", user="of_api")
                assert still_closed.returncode != 0

                release_c = str(uuid.uuid4())
                _activate_another(release_c)
                _publish_candidate(tmp_path, release_c)
                third = promote_candidate(ENVIRONMENT, release_c)
                assert third["release_id"] == release_c
                catalog = _cutover_catalog(ENVIRONMENT)
                assert catalog[SERVING_DATABASE] == "open"
                assert catalog[PREVIOUS_DATABASE] == "closed"
                assert catalog[EXPIRED_DATABASE] == "closed"
                _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=True)
                expired = _admin(
                    ENVIRONMENT,
                    EXPIRED_DATABASE,
                    "SELECT release_id::text FROM publish.active_dataset_release;",
                )
                _set_connections(ENVIRONMENT, EXPIRED_DATABASE, allow=False)
                assert release_a in expired
                restored = rollback_serving(ENVIRONMENT, release_b)
                assert restored["release_id"] == release_b
                catalog = _cutover_catalog(ENVIRONMENT)
                assert catalog[SERVING_DATABASE] == "open"
                assert catalog[CANDIDATE_DATABASE] == "open"
                assert catalog[PREVIOUS_DATABASE] is None
                assert catalog[EXPIRED_DATABASE] == "closed"
                kept = _admin(
                    ENVIRONMENT,
                    CANDIDATE_DATABASE,
                    "SELECT release_id::text FROM publish.active_dataset_release;",
                )
                assert release_c in kept
                with pytest.raises(SnapshotError, match="preserved serving generation"):
                    promote_candidate(ENVIRONMENT, release_c)
                assert release_c in _admin(
                    ENVIRONMENT,
                    CANDIDATE_DATABASE,
                    "SELECT release_id::text FROM publish.active_dataset_release;",
                )
                assert release_b in _admin(
                    ENVIRONMENT,
                    SERVING_DATABASE,
                    "SELECT release_id::text FROM publish.active_dataset_release;",
                )
                still_open = _run(ENVIRONMENT, SERVING_DATABASE, "SELECT 1;", user="of_api")
                assert still_open.returncode == 0
                again = rollback_serving(ENVIRONMENT, release_b)
                assert again["release_id"] == release_b
                assert _cutover_catalog(ENVIRONMENT)[EXPIRED_DATABASE] == "closed"
            finally:
                _clear_cutover_databases()
                if release_c:
                    discard_test_releases(ENVIRONMENT, [release_c])
                if release_b:
                    discard_test_releases(ENVIRONMENT, [release_b])
        assert _development_release() == dev_before
        assert _admin(ENVIRONMENT, "postgres", _ROLES) == roles_before
    finally:
        _clear_cutover_databases()


def _generation_oids() -> str:
    names = ", ".join(
        f"'{name}'"
        for name in (
            CANDIDATE_DATABASE,
            SERVING_DATABASE,
            PREVIOUS_DATABASE,
            EXPIRED_DATABASE,
        )
    )
    return _admin(
        ENVIRONMENT,
        "postgres",
        "SELECT datname || '|' || oid::text FROM pg_database "
        f"WHERE datname IN ({names}) ORDER BY 1;",
    )


def _publish_candidate(directory: Path, release_id: str) -> None:
    dump = create_snapshot(ENVIRONMENT, directory)
    assert dump.name == f"{release_id}.dump"
    restore_candidate(ENVIRONMENT, dump)


def _activate_another(release_id: str):
    manifest = _approved(load_json(CONTRACT_PATH), _row(), release_id)
    persist_manifest(ENVIRONMENT, manifest)
    promote_release(ENVIRONMENT, manifest, [_row()])
    return manifest


def _clear_cutover_databases() -> None:
    assert ENVIRONMENT == "test"
    for name in (EXPIRED_DATABASE, PREVIOUS_DATABASE, SERVING_DATABASE, CANDIDATE_DATABASE):
        _admin(
            ENVIRONMENT,
            "postgres",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid();",
        )
        _admin(ENVIRONMENT, "postgres", f"DROP DATABASE IF EXISTS {name};")


def _sleep_on(database: str) -> subprocess.Popen[str]:
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
            database,
            "--command",
            "SELECT pg_sleep(60);",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_session(database: str) -> None:
    for _attempt in range(25):
        if _session_count(database) != "0":
            return
        time.sleep(0.2)
    raise AssertionError("The sleeper did not connect.")


def _session_count(database: str) -> str:
    return _admin(
        ENVIRONMENT,
        "postgres",
        "SELECT count(*)::text FROM pg_stat_activity "
        f"WHERE datname = '{database}' AND pid <> pg_backend_pid();",
    ).strip()
