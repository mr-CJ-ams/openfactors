"""API password files stay in a private directory."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from ops.api_read import (
    ApiDatabase,
    ApiReadError,
    _password_directory_path,
    password_directory,
    prepare_password_directory,
    run_query,
    sweep_password_files,
)

from openfactors.api.catalog import open_catalog

_PASSWORD = "example-api-password"


def test_a_finished_query_removes_its_password_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(tmp_path / "pgpass"))
    captured: dict[str, Path] = {}

    class FakeProcess:
        def __init__(self, _command: list[str], **kwargs: object) -> None:
            env = kwargs["env"]
            assert isinstance(env, dict)
            path = Path(str(env["PGPASSFILE"]))
            captured["path"] = path
            assert path.is_file()
            assert _PASSWORD in path.read_text(encoding="utf-8")
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
            self.returncode: int | None = None

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            self.returncode = 0
            return "", ""

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("ops.api_read.psql_executable", lambda: "psql")
    monkeypatch.setattr("ops.api_read.subprocess.Popen", FakeProcess)
    run_query(
        _database(),
        "SELECT 1;",
        variables={},
        integers={},
        tuples_only=True,
    )
    path = captured["path"]
    assert path.parent.resolve() == (tmp_path / "pgpass").resolve()
    assert path.name.startswith("openfactors-api-")
    assert path.name.endswith(".pgpass")
    assert not path.exists()


def test_startup_removes_a_password_file_left_by_a_killed_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "pgpass"
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(directory))
    leftover = directory / "openfactors-api-deadbeef.pgpass"
    directory.mkdir()
    leftover.write_text("leftover\n", encoding="utf-8")
    prepare_password_directory()
    assert not leftover.exists()


def test_startup_keeps_unrelated_files_and_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "pgpass"
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(directory))
    directory.mkdir()
    notes = directory / "notes.txt"
    notes.write_text("keep", encoding="utf-8")
    almost = directory / "openfactors-api-.pgpass"
    almost.write_text("keep", encoding="utf-8")
    suffix = directory / "openfactors-api-ok.pgpass.txt"
    suffix.write_text("keep", encoding="utf-8")
    nested = directory / "openfactors-api-nested.pgpass"
    nested.mkdir()
    outside = tmp_path / "openfactors-api-outside.pgpass"
    outside.write_text("keep", encoding="utf-8")
    system_temp = Path(tempfile.gettempdir()) / "openfactors-api-system.pgpass"
    system_temp.write_text("keep", encoding="utf-8")
    target = directory / "notes.txt"
    try:
        link = directory / "openfactors-api-link.pgpass"
        link.symlink_to(target)
    except OSError:
        link = None
    try:
        sweep_password_files(prepare_password_directory())
        assert notes.read_text(encoding="utf-8") == "keep"
        assert almost.read_text(encoding="utf-8") == "keep"
        assert suffix.read_text(encoding="utf-8") == "keep"
        assert nested.is_dir()
        assert outside.read_text(encoding="utf-8") == "keep"
        assert system_temp.read_text(encoding="utf-8") == "keep"
        if link is not None:
            assert link.is_symlink()
            assert target.read_text(encoding="utf-8") == "keep"
    finally:
        system_temp.unlink(missing_ok=True)


def test_an_unset_password_directory_stays_the_runtime_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENFACTORS_API_PGPASS_DIR", raising=False)
    path = _password_directory_path()
    assert path.parts[-4:] == ("ops", "local", "runtime", "api-pgpass")
    assert path.resolve() != Path(tempfile.gettempdir()).resolve()


def test_password_directory_is_private_and_is_not_the_system_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(tmp_path / "pgpass"))
    directory = password_directory()
    assert directory.is_dir()
    assert directory.resolve() != Path(tempfile.gettempdir()).resolve()
    if os.name == "posix":
        assert directory.stat().st_mode & 0o777 == 0o700
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", tempfile.gettempdir())
    with pytest.raises(ApiReadError, match="system temporary directory"):
        password_directory()


def test_password_directory_rejects_a_symbolic_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "openfactors-api-secret.pgpass"
    secret.write_text("keep", encoding="utf-8")
    link = tmp_path / "pgpass-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(link))
    with pytest.raises(ApiReadError, match="cannot be a link"):
        prepare_password_directory()
    assert secret.read_text(encoding="utf-8") == "keep"


def test_a_second_process_cannot_remove_a_live_password_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "pgpass"
    directory.mkdir()
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(directory))
    live = directory / "openfactors-api-live.pgpass"
    live.write_text("keep", encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root)])
    env["OPENFACTORS_API_PGPASS_DIR"] = str(directory)
    proc = subprocess.Popen(
        [sys.executable, "-c", _OWNER],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = _read_ready(proc)
        if line != "ready\n":
            proc.kill()
            proc.wait(timeout=5)
            err = proc.stderr.read() if proc.stderr is not None else ""
            raise AssertionError(err or repr(line))
        with pytest.raises(ApiReadError, match="already in use"):
            prepare_password_directory()
        assert live.read_text(encoding="utf-8") == "keep"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    _prepare_after_owner_exits()
    assert not live.exists()


def test_lock_symlink_is_not_followed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outside = tmp_path / "outside-lock"
    outside.write_text("keep", encoding="utf-8")
    directory = tmp_path / "pgpass"
    directory.mkdir()
    lock = directory / ".openfactors-api.lock"
    try:
        lock.symlink_to(outside)
    except OSError:
        return
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(directory))
    if os.name == "posix":
        prepare_password_directory()
        assert lock.is_symlink()
    else:
        with pytest.raises(ApiReadError, match="cannot be a link"):
            prepare_password_directory()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_catalog_startup_uses_the_private_password_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "pgpass"
    monkeypatch.delenv("OPENFACTORS_API_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("OPENFACTORS_API_PGPASS_DIR", str(directory))
    monkeypatch.setenv("OPENFACTORS_API_EXPORT_DIR", str(tmp_path / "exports"))
    leftover = directory / "openfactors-api-startup.pgpass"
    directory.mkdir()
    leftover.write_text("leftover\n", encoding="utf-8")
    server = open_catalog("dev", "127.0.0.1", 0)
    try:
        assert not leftover.exists()
        if os.name == "posix":
            assert directory.stat().st_mode & 0o777 == 0o700
    finally:
        server.server_close()


_OWNER = """
from ops.api_read import password_directory
password_directory()
print("ready", flush=True)
input()
"""


def _prepare_after_owner_exits() -> None:
    """Take the directory back after the owner process has exited."""
    deadline = time.monotonic() + 2
    while True:
        try:
            prepare_password_directory()
            return
        except ApiReadError as exc:
            if "already in use" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _read_ready(proc: subprocess.Popen[str]) -> str:
    holder: list[str] = []

    def read() -> None:
        assert proc.stdout is not None
        holder.append(proc.stdout.readline())

    thread = threading.Thread(target=read)
    thread.start()
    thread.join(10)
    if thread.is_alive():
        return ""
    return holder[0]


def _database() -> ApiDatabase:
    return ApiDatabase(
        host="127.0.0.1",
        port=1,
        database="openfactors_dev",
        user="of_api",
        password=_PASSWORD,
        sslmode="require",
        sslrootcert=None,
    )
