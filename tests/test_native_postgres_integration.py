"""Disposable PostgreSQL 16 proof of the native restore path.

The container is created and removed by this test. It does not use the
development database, the VPS, or trust authentication.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.publish_snapshot import (
    PREVIOUS_DATABASE,
    REQUIRED_ROLES,
    SERVING_DATABASE,
    SnapshotError,
    _create_candidate,
    _database_comment,
    _database_exists,
    _generation_lock,
    _guard_generation_lock,
    _mark_authenticated_candidate,
    _published_identity,
    _restore_dump_file,
    _run,
    assert_cluster_roles,
    drop_database,
    promote_candidate,
    rollback_serving,
    validate_candidate,
)

from openfactors.quality.manifest import content_sha256

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "native_candidate.sql"
IMAGE = "postgres:16.10-bookworm"
HBA = """\
local   all             postgres                                peer
local   all             of_restore                              peer
local   all             all                                     reject
host    all             all             0.0.0.0/0               reject
host    all             all             ::/0                    reject
"""
RELEASE_A = "11111111-1111-4111-8111-111111111111"
RELEASE_B = "22222222-2222-4222-8222-222222222222"


def _docker(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AssertionError(detail or "A disposable PostgreSQL command failed.")
    return completed


@pytest.fixture(scope="module")
def native_cluster() -> str:
    name = f"of-native-it-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    password = os.urandom(16).hex()
    _docker(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=reject",
            IMAGE,
        ]
    )
    try:
        ready = False
        for _attempt in range(60):
            probe = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise AssertionError("PostgreSQL 16 did not become ready.")
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".conf",
            delete=False,
        ) as handle:
            handle.write(HBA)
            hba = Path(handle.name)
        try:
            _docker(["docker", "cp", str(hba), f"{name}:/var/lib/postgresql/data/pg_hba.conf"])
            _docker(
                [
                    "docker",
                    "exec",
                    "-u",
                    "root",
                    name,
                    "chown",
                    "postgres:postgres",
                    "/var/lib/postgresql/data/pg_hba.conf",
                ]
            )
            _docker(
                [
                    "docker",
                    "exec",
                    "-u",
                    "root",
                    name,
                    "chmod",
                    "600",
                    "/var/lib/postgresql/data/pg_hba.conf",
                ]
            )
        finally:
            hba.unlink(missing_ok=True)
        _docker(
            [
                "docker",
                "exec",
                "-u",
                "postgres",
                name,
                "psql",
                "--dbname",
                "postgres",
                "--command",
                "SELECT pg_reload_conf();",
            ]
        )
        rules = _docker(["docker", "exec", name, "cat", "/var/lib/postgresql/data/pg_hba.conf"])
        if "trust" in rules.stdout.lower():
            raise AssertionError("The disposable cluster still allows trust authentication.")
        _docker(
            [
                "docker",
                "exec",
                "-u",
                "root",
                name,
                "useradd",
                "--system",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "of_restore",
            ]
        )
        _docker(
            [
                "docker",
                "cp",
                str(ROOT / "ops" / "sql" / "bootstrap_serving_roles.sql"),
                f"{name}:/tmp/bootstrap_serving_roles.sql",
            ]
        )
        _docker(
            [
                "docker",
                "exec",
                "-u",
                "postgres",
                name,
                "psql",
                "--dbname",
                "postgres",
                "--set",
                "ON_ERROR_STOP=1",
                "--file",
                "/tmp/bootstrap_serving_roles.sql",
            ]
        )
        _docker(
            [
                "docker",
                "exec",
                "-u",
                "postgres",
                name,
                "psql",
                "--dbname",
                "postgres",
                "--command",
                "CREATE DATABASE openfactors_dev;",
            ]
        )
        previous = {
            key: os.environ.get(key)
            for key in (
                "OPENFACTORS_DB_BACKEND",
                "OPENFACTORS_NATIVE_TEST_HARNESS",
                "OPENFACTORS_NATIVE_DOCKER_CONTAINER",
            )
        }
        os.environ["OPENFACTORS_DB_BACKEND"] = "native"
        os.environ["OPENFACTORS_NATIVE_TEST_HARNESS"] = "1"
        os.environ["OPENFACTORS_NATIVE_DOCKER_CONTAINER"] = name
        try:
            yield name
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _dump(name: str, release_id: str) -> str:
    version = str(load_json(CONTRACT_PATH)["dataset_version"])
    digest = content_sha256([])
    sql = (
        FIXTURE.read_text(encoding="utf-8")
        .replace("__RELEASE_ID__", release_id)
        .replace("__CONTENT_HASH__", digest)
        .replace("__SCHEMA_VERSION__", version)
    )
    inside = f"/tmp/{release_id}.sql"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".sql",
        delete=False,
    ) as handle:
        handle.write(sql)
        host = Path(handle.name)
    try:
        _docker(["docker", "cp", str(host), f"{name}:{inside}"])
    finally:
        host.unlink(missing_ok=True)
    _docker(
        [
            "docker",
            "exec",
            "-u",
            "postgres",
            name,
            "psql",
            "--dbname",
            "postgres",
            "--command",
            "CREATE DATABASE openfactors_fixture;",
        ]
    )
    _docker(
        [
            "docker",
            "exec",
            "-u",
            "postgres",
            name,
            "psql",
            "--dbname",
            "openfactors_fixture",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            inside,
        ]
    )
    dump = f"/tmp/{release_id}.dump"
    _docker(
        [
            "docker",
            "exec",
            "-u",
            "postgres",
            name,
            "pg_dump",
            "--dbname",
            "openfactors_fixture",
            "--format",
            "custom",
            "--file",
            dump,
        ]
    )
    _docker(["docker", "exec", "-u", "root", name, "chmod", "644", dump])
    _docker(
        [
            "docker",
            "exec",
            "-u",
            "postgres",
            name,
            "psql",
            "--dbname",
            "postgres",
            "--command",
            "DROP DATABASE openfactors_fixture WITH (FORCE);",
        ]
    )
    return dump


def _manifest(release_id: str) -> dict[str, object]:
    return {
        "release_id": release_id,
        "dataset_version": str(load_json(CONTRACT_PATH)["dataset_version"]),
        "row_count": 0,
        "content_sha256_hex": content_sha256([]),
    }


def _publish(name: str, release_id: str) -> None:
    dump = _dump(name, release_id)
    with _generation_lock("serving"):
        _create_candidate("serving")
        try:
            _restore_dump_file("serving", Path(dump))
            validate_candidate("serving", _manifest(release_id))
            _mark_authenticated_candidate("serving")
        except Exception:
            drop_database("serving", "openfactors_candidate")
            raise


def test_native_restore_ownership_lock_promotion_and_rollback(native_cluster: str) -> None:
    root = subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "root",
            native_cluster,
            "psql",
            "--username",
            "of_restore",
            "--dbname",
            "postgres",
            "--no-password",
            "--command",
            "SELECT 1;",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert root.returncode != 0

    assert_cluster_roles("serving", REQUIRED_ROLES)
    denied = _run(
        "serving",
        "postgres",
        "CREATE DATABASE openfactors_should_not;",
        user="of_api",
    )
    assert denied.returncode != 0
    assert "permission denied" in (denied.stderr or "")
    assert _database_exists("serving", "openfactors_dev")
    with pytest.raises(SnapshotError, match="allow-listed candidate"):
        drop_database("serving", "openfactors_dev")
    assert _database_exists("serving", "openfactors_dev")

    with _generation_lock("serving"):
        with pytest.raises(SnapshotError, match="Another generation command is running"):
            with _generation_lock("serving"):
                pass
    with _generation_lock("serving") as held:
        held.lose_session()
        with pytest.raises(SnapshotError, match="generation lock was lost"):
            _guard_generation_lock("serving", required=True)

    with _generation_lock("serving"):
        _create_candidate("serving")
        try:
            _restore_dump_file("serving", Path("/tmp/not-a-dump"))
            raise AssertionError("A damaged dump was restored.")
        except SnapshotError:
            drop_database("serving", "openfactors_candidate")
    assert not _database_exists("serving", "openfactors_candidate")
    assert _database_exists("serving", "openfactors_dev")

    _publish(native_cluster, RELEASE_A)
    promoted = promote_candidate("serving", RELEASE_A)
    assert promoted["database"] == SERVING_DATABASE
    assert promoted["release_id"] == RELEASE_A
    assert not _database_exists("serving", "openfactors_candidate")

    _publish(native_cluster, RELEASE_B)
    second = promote_candidate("serving", RELEASE_B)
    assert second["release_id"] == RELEASE_B
    assert _database_exists("serving", PREVIOUS_DATABASE)

    restored = rollback_serving("serving", RELEASE_A)
    assert restored["database"] == SERVING_DATABASE
    assert restored["release_id"] == RELEASE_A
    assert _published_identity("serving", SERVING_DATABASE)["release_id"] == RELEASE_A
    assert _database_comment("serving", "openfactors_candidate") == "openfactors-demoted"
    assert _database_exists("serving", "openfactors_dev")
    assert not _database_exists("serving", PREVIOUS_DATABASE)


def test_of_restore_can_read_only_the_handoff_dump(native_cluster: str) -> None:
    """Inside the disposable container, of_restore reads one dump and nothing else."""
    setup = """
set -eu
rm -rf /var/openfactors/restore
mkdir -p /var/openfactors/restore/private
printf 'secret' > /var/openfactors/restore/private/secret.dump
chmod 0700 /var/openfactors/restore/private
chmod 0600 /var/openfactors/restore/private/secret.dump
mkdir -p /var/openfactors/restore/sample.handoff
printf 'dump-bytes' > /var/openfactors/restore/sample.handoff/sample.dump
chown root:of_restore /var/openfactors/restore
chown root:of_restore /var/openfactors/restore/sample.handoff
chown root:of_restore /var/openfactors/restore/sample.handoff/sample.dump
chmod 0710 /var/openfactors/restore
chmod 0550 /var/openfactors/restore/sample.handoff
chmod 0440 /var/openfactors/restore/sample.handoff/sample.dump
"""
    _docker(["docker", "exec", "-u", "root", native_cluster, "bash", "-c", setup])

    def as_restore(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", "-u", "of_restore", native_cluster, "bash", "-c", command],
            text=True,
            capture_output=True,
            check=False,
        )

    secret = as_restore("cat /var/openfactors/restore/private/secret.dump")
    listing = as_restore("ls /var/openfactors/restore")
    dump = as_restore("cat /var/openfactors/restore/sample.handoff/sample.dump")
    append = as_restore("printf x >> /var/openfactors/restore/sample.handoff/sample.dump")
    extra = as_restore("touch /var/openfactors/restore/sample.handoff/extra")
    assert secret.returncode != 0
    assert listing.returncode != 0
    assert dump.returncode == 0
    assert dump.stdout == "dump-bytes"
    assert append.returncode != 0
    assert extra.returncode != 0
    unchanged = _docker(
        [
            "docker",
            "exec",
            "-u",
            "root",
            native_cluster,
            "cat",
            "/var/openfactors/restore/sample.handoff/sample.dump",
        ]
    )
    assert unchanged.stdout == "dump-bytes"
    _docker(
        [
            "docker",
            "exec",
            "-u",
            "root",
            native_cluster,
            "rm",
            "-rf",
            "/var/openfactors/restore",
        ]
    )


def test_native_commands_are_documented_without_trust() -> None:
    guide = (ROOT / "docs" / "decisions" / "native-postgres-serving.md").read_text(encoding="utf-8")
    assert "local   all   of_restore   peer" in guide
    assert "runuser -u of_restore" in guide
    assert "Do not use trust" in guide
    assert "Public Beta v0.1 stays no-go" in guide
    assert "OPENFACTORS_DB_BACKEND=native" in guide
    assert "0710" in guide
    assert "0440" in guide
    assert ".handoff" in guide
    assert "0777" in guide
