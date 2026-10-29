"""Native backend selection, peer identity, and Compose compatibility."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from ops import db_backend
from ops.publish_snapshot import SnapshotError, restore_accepted, restore_candidate

RELEASE = "11111111-1111-4111-8111-111111111111"


def test_default_backend_is_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENFACTORS_DB_BACKEND", raising=False)
    assert db_backend.backend_name() == "compose"


def test_unknown_backend_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENFACTORS_DB_BACKEND", "trust")
    with pytest.raises(SnapshotError, match="compose or native"):
        restore_accepted("staging", RELEASE)


def test_serving_restore_requires_the_native_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENFACTORS_DB_BACKEND", raising=False)
    with pytest.raises(SnapshotError, match="native backend"):
        restore_accepted("serving", RELEASE)


def test_dev_restore_accepted_stays_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENFACTORS_DB_BACKEND", raising=False)
    with pytest.raises(SnapshotError, match="staging cluster"):
        restore_accepted("dev", RELEASE)


def test_serving_unsigned_restore_stays_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENFACTORS_DB_BACKEND", "native")
    with pytest.raises(SnapshotError, match="restore-accepted"):
        restore_candidate("serving", Path("unused.dump"))


def test_root_uses_runuser_and_another_user_is_refused() -> None:
    assert db_backend.identity_prefix(euid=0, target_uid=40) == [
        "runuser",
        "-u",
        "of_restore",
        "--",
    ]
    assert db_backend.identity_prefix(euid=40, target_uid=40) == []
    with pytest.raises(db_backend.BackendError, match="of_restore OS user"):
        db_backend.identity_prefix(euid=1000, target_uid=40)


def test_native_client_has_no_trust_and_refuses_other_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        db_backend,
        "connection_prefix",
        lambda: ["runuser", "-u", "of_restore", "--"],
    )
    monkeypatch.delenv("OPENFACTORS_NATIVE_DOCKER_CONTAINER", raising=False)
    argv = db_backend.native_psql_argv("postgres", interactive=False)
    assert argv[:4] == ["runuser", "-u", "of_restore", "--"]
    assert "--no-password" in argv
    assert argv[argv.index("--username") + 1] == "of_restore"
    assert "trust" not in argv
    with pytest.raises(db_backend.BackendError, match="database name"):
        db_backend.native_psql_argv("openfactors_dev", interactive=False)


def test_harness_is_ignored_unless_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENFACTORS_NATIVE_DOCKER_CONTAINER", "of-native-it")
    monkeypatch.delenv("OPENFACTORS_NATIVE_TEST_HARNESS", raising=False)
    with pytest.raises(db_backend.BackendError, match="not enabled"):
        db_backend.connection_prefix()


def test_compose_query_still_uses_of_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENFACTORS_DB_BACKEND", raising=False)
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("ops.publish_snapshot.subprocess.run", fake_run)
    monkeypatch.setattr(
        "ops.publish_snapshot.compose_base", lambda _environment: ["docker", "compose"]
    )
    from ops.publish_snapshot import _text

    assert _text("test", "postgres", "SELECT 1;") == "ok"
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--username") + 1] == "of_admin"
    assert "runuser" not in argv
    assert seen["input"] == "SELECT 1;"


def test_bootstrap_sql_keeps_of_api_read_only() -> None:
    text = Path("ops/sql/bootstrap_serving_roles.sql").read_text(encoding="utf-8")
    privileges = Path("ops/sql/serving_restore_privileges.sql").read_text(encoding="utf-8")
    assert "trust" not in text.lower()
    assert "CREATE ROLE of_restore LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOINHERIT;" in text
    assert "WITH ADMIN OPTION" not in text
    assert "GRANT of_migrate TO of_restore;" in text
    assert "GRANT of_api TO of_restore;" in text
    for line in privileges.splitlines():
        code = line.split("--", 1)[0].upper()
        assert "INSERT" not in code
        assert "CREATEDB" not in code
        assert "SUPERUSER" not in code
