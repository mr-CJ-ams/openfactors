"""Serving restore checks a pinned signature before it creates a database."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops import publish_snapshot
from ops.publish_snapshot import (
    _AUTHENTICATED_MARK,
    _DEMOTED_MARK,
    CANDIDATE_DATABASE,
    DATASET_ID,
    EXCLUDED_SCHEMAS,
    EXCLUDED_TABLES,
    PREVIOUS_DATABASE,
    SERVING_DATABASE,
    SIGNATURE_NAMESPACE,
    SIGNATURE_PRINCIPAL,
    SnapshotError,
    _active_generation_lock,
    _cleanup_report,
    _create_candidate,
    _mark_authenticated_candidate,
    _refuse_demoted_candidate,
    _require_serving_pin,
    _restore_private_candidate,
    main,
    restore_accepted,
    restore_candidate,
)

RELEASE = "11111111-1111-4111-8111-111111111111"
_MARKER = f'namespaces="{SIGNATURE_NAMESPACE}"'
_HOST = sys.platform


class _Stat:
    def __init__(self, mode: int, uid: int, *, dev: int = 1, ino: int = 1, size: int = 0) -> None:
        self.st_mode = mode
        self.st_uid = uid
        self.st_gid = 0
        self.st_dev = dev
        self.st_ino = ino
        self.st_size = size
        self.st_nlink = 1


class _Dir:
    def __init__(self, path: Path, files: dict[str, object], info: _Stat) -> None:
        self.path = path
        self.files = files
        self.info = info


class _OpenFile:
    def __init__(self, data: bytes, info: _Stat) -> None:
        self.data = data
        self.pos = 0
        self.info = info


class _Descriptors:
    """Stand in for Linux directory descriptors on a development host without them.

    File bytes are copied when the directory is opened, and the original files
    are not held open. A later rename of that directory cannot change the copy.
    """

    def __init__(self) -> None:
        self._next_fd = 20_000
        self.dirs: dict[int, _Dir] = {}
        self.files: dict[int, _OpenFile] = {}
        self.real_open = os.open
        self.real_close = os.close
        self.real_read = os.read
        self.real_write = os.write
        self.real_lseek = os.lseek
        self.real_fsync = os.fsync
        self.real_fstat = os.fstat
        self.real_listdir = os.listdir
        self.real_stat = os.stat
        self.real_mkdir = os.mkdir
        self.real_chmod = os.chmod
        self.real_lstat = os.lstat

    def install(self, monkeypatch: pytest.MonkeyPatch, restore_root: Path) -> None:
        self.restore_root = restore_root
        monkeypatch.setattr(os, "open", self.open)
        monkeypatch.setattr(os, "close", self.close)
        monkeypatch.setattr(os, "read", self.read)
        monkeypatch.setattr(os, "write", self.write)
        monkeypatch.setattr(os, "lseek", self.lseek)
        monkeypatch.setattr(os, "fsync", self.fsync)
        monkeypatch.setattr(os, "fstat", self.fstat)
        monkeypatch.setattr(os, "listdir", self.listdir)
        monkeypatch.setattr(os, "stat", self.stat)
        monkeypatch.setattr(os, "mkdir", self.mkdir)
        monkeypatch.setattr(os, "chmod", self.chmod)

    def open(
        self, path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        directory = flags & getattr(os, "O_DIRECTORY", 0)
        nofollow = flags & getattr(os, "O_NOFOLLOW", 0)
        if dir_fd is None and not directory:
            return self.real_open(path, flags, mode)
        full = Path(path) if dir_fd is None else self.dirs[dir_fd].path / os.fspath(path)
        if nofollow and full.is_symlink():
            raise OSError(errno_loop(), "symlink")
        if directory:
            if flags & os.O_CREAT:
                self.real_mkdir(full, mode)
            return self._open_dir(full)
        if flags & os.O_CREAT:
            return self.real_open(
                full,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                mode,
            )
        if dir_fd is None:
            raise OSError(errno.EBADF, "missing directory")
        entry = self.dirs[dir_fd].files[os.fspath(path)]
        if entry == "symlink":
            raise OSError(errno_loop(), "symlink")
        if not isinstance(entry, tuple):
            raise OSError(errno.ENOENT, "not a file")
        data, info = entry
        fd = self._next_fd
        self._next_fd += 1
        self.files[fd] = _OpenFile(data, info)
        return fd

    def close(self, fd: int) -> None:
        if fd in self.files:
            del self.files[fd]
            return
        if fd in self.dirs:
            del self.dirs[fd]
            return
        self.real_close(fd)

    def read(self, fd: int, size: int) -> bytes:
        item = self.files.get(fd)
        if item is None:
            return self.real_read(fd, size)
        chunk = item.data[item.pos : item.pos + size]
        item.pos += len(chunk)
        return chunk

    def write(self, fd: int, data: bytes) -> int:
        if fd in self.files:
            raise OSError(errno.EBADF, "read only")
        return self.real_write(fd, data)

    def lseek(self, fd: int, position: int, how: int) -> int:
        item = self.files.get(fd)
        if item is None:
            return self.real_lseek(fd, position, how)
        if how == os.SEEK_SET:
            item.pos = position
        elif how == os.SEEK_CUR:
            item.pos += position
        else:
            item.pos = len(item.data) + position
        return item.pos

    def fsync(self, fd: int) -> None:
        if fd in self.files or fd in self.dirs:
            return
        self.real_fsync(fd)

    def fstat(self, fd: int) -> os.stat_result | _Stat:
        item = self.files.get(fd)
        if item is not None:
            return item.info
        directory = self.dirs.get(fd)
        if directory is not None:
            return directory.info
        return self.real_fstat(fd)

    def listdir(self, fd: int) -> list[str]:
        directory = self.dirs.get(fd)
        if directory is None:
            return list(self.real_listdir(fd))
        return list(directory.files)

    def stat(
        self,
        path: str | Path,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result | _Stat:
        if dir_fd is None:
            return self.real_stat(path, follow_symlinks=follow_symlinks)
        entry = self.dirs[dir_fd].files[os.fspath(path)]
        if entry == "symlink":
            return _Stat(stat.S_IFLNK | 0o777, 4242)
        if isinstance(entry, tuple):
            return entry[1]
        return _Stat(stat.S_IFDIR | 0o755, 4242)

    def mkdir(self, path: str | Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if dir_fd is None:
            self.real_mkdir(path, mode)
            return
        self.real_mkdir(self.dirs[dir_fd].path / os.fspath(path), mode)

    def chmod(
        self,
        path: str | Path,
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if dir_fd is None:
            self.real_chmod(path, mode)
            return
        full = self.dirs[dir_fd].path / os.fspath(path)
        if follow_symlinks is False and full.is_symlink():
            raise OSError(errno_loop(), "symlink")
        self.real_chmod(full, mode)

    def _open_dir(self, full: Path) -> int:
        if full.is_symlink():
            raise OSError(errno_loop(), "symlink")
        if not full.is_dir():
            raise OSError(errno.ENOTDIR, "not a directory")
        files: dict[str, object] = {}
        for name in self.real_listdir(full):
            child = full / name
            if child.is_symlink():
                files[name] = "symlink"
            elif child.is_file():
                info = self.real_lstat(child)
                files[name] = (
                    child.read_bytes(),
                    _Stat(
                        stat.S_IFREG | 0o444,
                        info.st_uid,
                        dev=info.st_dev,
                        ino=info.st_ino,
                        size=info.st_size,
                    ),
                )
            else:
                files[name] = "other"
        info = self.real_lstat(full)
        under_restore = full == self.restore_root or self.restore_root in full.parents
        mode = stat.S_IFDIR | (0o700 if under_restore else 0o550)
        uid = 0 if under_restore else 4242
        fd = self._next_fd
        self._next_fd += 1
        self.dirs[fd] = _Dir(
            full,
            files,
            _Stat(mode, uid, dev=info.st_dev, ino=info.st_ino),
        )
        return fd


def errno_loop() -> int:
    return errno.ELOOP


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database")

    for name in (
        "_admin",
        "_create_candidate",
        "drop_database",
        "_database_exists",
        "_generation_lock",
        "validate_candidate",
        "_pg_restore",
        "_copy_to_container",
        "assert_cluster_roles",
    ):
        monkeypatch.setattr(publish_snapshot, name, boom)


@pytest.fixture
def serving_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    accepted_root = tmp_path / "accepted"
    pin = tmp_path / "pin" / "allowed_signers"
    restore = tmp_path / "restore"
    accepted_root.mkdir()
    pin.parent.mkdir()
    restore.mkdir(mode=0o700)
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        raise AssertionError("ssh-keygen is not available.")
    monkeypatch.setattr(publish_snapshot, "_ssh_keygen", lambda: keygen)
    monkeypatch.setattr(publish_snapshot, "ACCEPTED_ROOT", accepted_root)
    monkeypatch.setattr(publish_snapshot, "PIN_PATH", pin)
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", restore)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    if not getattr(os, "O_DIRECTORY", 0):
        monkeypatch.setattr(os, "O_DIRECTORY", 0x10000, raising=False)
    if not getattr(os, "O_NOFOLLOW", 0):
        monkeypatch.setattr(os, "O_NOFOLLOW", 0x20000, raising=False)
    real_lstat = os.lstat

    def lstat(path: str | Path) -> os.stat_result | _Stat:
        target = Path(path)
        info = real_lstat(path)
        if target == pin:
            return _Stat(
                stat.S_IFREG | 0o440, 0, size=info.st_size, dev=info.st_dev, ino=info.st_ino
            )
        if target == restore:
            return _Stat(stat.S_IFDIR | 0o700, 0, dev=info.st_dev, ino=info.st_ino)
        if target.parent == accepted_root and stat.S_ISLNK(info.st_mode):
            return info
        if target.parent == accepted_root and stat.S_ISDIR(info.st_mode):
            return _Stat(stat.S_IFDIR | 0o550, 4242, dev=info.st_dev, ino=info.st_ino)
        if _is_ancestor(target, pin):
            return _Stat(stat.S_IFDIR | 0o755, 0, dev=info.st_dev, ino=info.st_ino)
        return info

    monkeypatch.setattr(os, "lstat", lstat)
    if _HOST == "linux":
        real_fstat = os.fstat

        def fstat(fd: int) -> os.stat_result | _Stat:
            info = real_fstat(fd)
            try:
                link = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                return info
            if stat.S_ISDIR(info.st_mode):
                try:
                    linked = link.resolve()
                    private_root = restore.resolve()
                except OSError:
                    return info
                if linked == private_root or private_root in linked.parents:
                    return _Stat(stat.S_IFDIR | 0o700, 0, dev=info.st_dev, ino=info.st_ino)
            return info

        monkeypatch.setattr(os, "fstat", fstat)
    else:
        _Descriptors().install(monkeypatch, restore)
    return {"accepted": accepted_root, "pin": pin, "restore": restore}


def _is_ancestor(path: Path, child: Path) -> bool:
    parent = child.parent
    while parent != parent.parent:
        if path == parent:
            return True
        parent = parent.parent
    return path == parent


def _manifest_bytes(release_id: str, dump: bytes) -> bytes:
    version = str(load_json(CONTRACT_PATH)["dataset_version"])
    payload = {
        "content_sha256_hex": "cd" * 32,
        "dataset_id": DATASET_ID,
        "dataset_version": version,
        "dump_format": "custom",
        "dump_sha256_hex": hashlib.sha256(dump).hexdigest(),
        "excluded_schemas": list(EXCLUDED_SCHEMAS),
        "excluded_tables": list(EXCLUDED_TABLES),
        "release_id": release_id,
        "row_count": 3,
        "source_database": "openfactors_dev",
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _key(directory: Path) -> Path:
    path = directory / "publisher"
    completed = subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-f",
            str(path),
            "-N",
            "",
            "-C",
            SIGNATURE_PRINCIPAL,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0 or not path.is_file():
        raise AssertionError("The test signing key could not be created.")
    return path


def _sign(manifest: Path, key_path: Path) -> bytes:
    completed = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(key_path),
            "-n",
            SIGNATURE_NAMESPACE,
            str(manifest),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    signature = manifest.parent / f"{manifest.name}.sig"
    if completed.returncode != 0 or not signature.is_file():
        raise AssertionError("The test signature could not be created.")
    payload = signature.read_bytes()
    signature.unlink()
    return payload


def _publish(paths: dict[str, Path], dump: bytes, *, release_id: str = RELEASE) -> Path:
    directory = paths["accepted"] / release_id
    directory.mkdir()
    manifest = _manifest_bytes(release_id, dump)
    staged = paths["restore"].parent / "stage"
    staged.mkdir(exist_ok=True)
    manifest_path = staged / f"{release_id}.json"
    manifest_path.write_bytes(manifest)
    key = _key(staged)
    signature = _sign(manifest_path, key)
    public = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    paths["pin"].write_text(f"{SIGNATURE_PRINCIPAL} {_MARKER} {public}\n", encoding="utf-8")
    (directory / f"{release_id}.dump").write_bytes(dump)
    (directory / f"{release_id}.json").write_bytes(manifest)
    (directory / f"{release_id}.json.sig").write_bytes(signature)
    (directory / "READY").write_bytes(f"openfactors-accepted\n{release_id}\n".encode("ascii"))
    return directory


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def stop(environment: str, private: object) -> dict[str, object]:
        captured["environment"] = environment
        captured["dump"] = private.dump.read_bytes()  # type: ignore[attr-defined]
        captured["digest"] = private.digest  # type: ignore[attr-defined]
        captured["root"] = private.root  # type: ignore[attr-defined]
        return {"candidate_database": CANDIDATE_DATABASE}

    monkeypatch.setattr(publish_snapshot, "_restore_private_candidate", stop)
    return captured


def test_dev_and_test_are_refused_before_any_file_is_opened() -> None:
    with pytest.raises(SnapshotError, match="staging cluster"):
        restore_accepted("dev", RELEASE)
    with pytest.raises(SnapshotError, match="staging cluster"):
        restore_accepted("test", RELEASE)


def test_windows_and_a_non_root_caller_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    no_database: None,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SnapshotError, match="Linux"):
        restore_accepted("staging", RELEASE)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(SnapshotError, match="root"):
        restore_accepted("staging", RELEASE)


def test_an_uppercase_release_id_is_refused(no_database: None) -> None:
    with pytest.raises(SnapshotError, match="lowercase UUID"):
        restore_accepted("staging", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE")


def test_environment_variables_do_not_select_paths(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
) -> None:
    dump = b"PGDMP-openfactors"
    _publish(serving_host, dump)
    evil = serving_host["restore"].parent / "evil-pin"
    evil.write_text("untrusted\n", encoding="utf-8")
    monkeypatch.setenv(
        "OPENFACTORS_ACCEPT_ROOT", str(serving_host["restore"].parent / "evil-accept")
    )
    monkeypatch.setenv("OPENFACTORS_ALLOWED_SIGNERS", str(evil))
    monkeypatch.setenv(
        "OPENFACTORS_RESTORE_ROOT", str(serving_host["restore"].parent / "evil-restore")
    )
    captured = _capture(monkeypatch)
    restore_accepted("staging", RELEASE)
    assert captured["dump"] == dump


def test_a_signed_release_is_copied_before_the_database(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
) -> None:
    dump = b"PGDMP-openfactors-authentic"
    directory = _publish(serving_host, dump)
    captured = _capture(monkeypatch)
    evidence = restore_accepted("staging", RELEASE)
    assert evidence["candidate_database"] == CANDIDATE_DATABASE
    assert captured["dump"] == dump
    assert captured["environment"] == "staging"
    assert not Path(str(captured["root"])).exists()
    assert (directory / "READY").is_file()


def test_a_tampered_dump_signature_or_manifest_stops_before_create(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
) -> None:
    directory = _publish(serving_host, b"PGDMP-original")
    dump = directory / f"{RELEASE}.dump"
    with dump.open("r+b") as handle:
        handle.write(b"X")
    with pytest.raises(SnapshotError, match="checksum"):
        restore_accepted("staging", RELEASE)
    dump.write_bytes(b"PGDMP-original")
    signature = directory / f"{RELEASE}.json.sig"
    payload = bytearray(signature.read_bytes())
    payload[-5] ^= 0x01
    signature.write_bytes(payload)
    with pytest.raises(SnapshotError, match="signature"):
        restore_accepted("staging", RELEASE)
    assert list(serving_host["restore"].iterdir()) == []
    assert (directory / "READY").is_file()


def test_ready_and_the_file_set_must_be_exact(
    serving_host: dict[str, Path],
    no_database: None,
) -> None:
    directory = _publish(serving_host, b"PGDMP-ready")
    ready = directory / "READY"
    ready.write_bytes(b"openfactors-accepted\n")
    with pytest.raises(SnapshotError, match="READY"):
        restore_accepted("staging", RELEASE)
    ready.write_bytes(f"openfactors-accepted\n{RELEASE}\n".encode("ascii"))
    (directory / "EXTRA").write_bytes(b"nope")
    with pytest.raises(SnapshotError, match="expected files"):
        restore_accepted("staging", RELEASE)


def test_a_symlink_release_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
    tmp_path: Path,
) -> None:
    directory = _publish(serving_host, b"PGDMP-link")
    aside = tmp_path / "aside"
    directory.rename(aside)
    try:
        os.symlink(aside, directory, target_is_directory=True)
    except OSError:
        directory.mkdir()
        current = os.lstat

        def lstat(path: str | Path) -> os.stat_result | _Stat:
            if Path(path) == directory:
                return _Stat(stat.S_IFLNK | 0o777, 4242)
            return current(path)

        monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(SnapshotError, match="real directory"):
        restore_accepted("staging", RELEASE)
    assert not any(serving_host["restore"].iterdir())


def test_a_symlink_inside_the_release_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
    tmp_path: Path,
) -> None:
    directory = _publish(serving_host, b"PGDMP-inner")
    dump = directory / f"{RELEASE}.dump"
    target = tmp_path / "other-dump"
    target.write_bytes(b"other")
    dump.unlink()
    try:
        os.symlink(target, dump)
    except OSError:
        dump.write_bytes(b"PGDMP-inner")
        current = os.stat

        def stat_entry(
            path: str | Path,
            *args: object,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result | _Stat:
            if dir_fd is not None and os.fspath(path) == f"{RELEASE}.dump":
                return _Stat(stat.S_IFLNK | 0o777, 4242)
            return current(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "stat", stat_entry)
    with pytest.raises(SnapshotError, match="symlink"):
        restore_accepted("staging", RELEASE)


def test_replacing_the_directory_does_not_change_the_copied_bytes(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
) -> None:
    original = b"PGDMP-original-directory"
    directory = _publish(serving_host, original)
    captured = _capture(monkeypatch)
    current = os.listdir
    swapped = {"done": False}

    def listdir(fd: int) -> list[str]:
        names = current(fd)
        if not swapped["done"]:
            swapped["done"] = True
            aside = directory.parent / "aside"
            directory.rename(aside)
            replacement = directory.parent / RELEASE
            replacement.mkdir()
            (replacement / f"{RELEASE}.dump").write_bytes(b"PGDMP-replaced")
            (replacement / f"{RELEASE}.json").write_bytes(b"{}\n")
            (replacement / f"{RELEASE}.json.sig").write_bytes(b"replaced")
            (replacement / "READY").write_bytes(b"openfactors-accepted\nnope\n")
        return names

    monkeypatch.setattr(os, "listdir", listdir)
    restore_accepted("staging", RELEASE)
    assert captured["dump"] == original


def test_a_dump_that_changes_while_it_is_copied_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
) -> None:
    dump = b"PGDMP-stable-bytes"
    directory = _publish(serving_host, dump)
    current = publish_snapshot._read_fd
    mutated = {"done": False}

    def read_fd(handle: int) -> bytes:
        blob = current(handle)
        if blob == dump and not mutated["done"]:
            mutated["done"] = True
            if _HOST == "linux":
                descriptor = os.open(
                    directory / f"{RELEASE}.dump",
                    os.O_WRONLY | getattr(os, "O_BINARY", 0),
                )
                try:
                    os.write(descriptor, b"X")
                finally:
                    os.close(descriptor)
            return blob
        if mutated["done"] and blob == dump and _HOST != "linux":
            return b"X" + blob[1:]
        return blob

    monkeypatch.setattr(publish_snapshot, "_read_fd", read_fd)
    with pytest.raises(SnapshotError, match="changed"):
        restore_accepted("staging", RELEASE)
    assert mutated["done"] is True
    assert list(serving_host["restore"].iterdir()) == []


def test_a_pin_the_acceptance_user_can_replace_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin = tmp_path / "allowed_signers"
    pin.write_text("pin\n", encoding="utf-8")
    monkeypatch.setattr(publish_snapshot, "PIN_PATH", pin)

    def lstat(path: str | Path) -> _Stat:
        target = Path(path)
        if target == pin:
            return _Stat(stat.S_IFREG | 0o440, 0, size=4)
        if target == pin.parent:
            return _Stat(stat.S_IFDIR | 0o755, 4242)
        return _Stat(stat.S_IFDIR | 0o755, 0)

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(SnapshotError, match="replace"):
        _require_serving_pin(4242)


def test_a_symlink_pin_and_a_parent_path_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin = tmp_path / "allowed_signers"
    pin.write_text("pin\n", encoding="utf-8")
    monkeypatch.setattr(publish_snapshot, "PIN_PATH", pin)
    monkeypatch.setattr(os, "lstat", lambda _path: _Stat(stat.S_IFLNK | 0o777, 0))
    with pytest.raises(SnapshotError, match="regular file"):
        _require_serving_pin(4242)
    monkeypatch.setattr(
        publish_snapshot,
        "PIN_PATH",
        Path("/var/openfactors/pin/../allowed_signers"),
    )
    with pytest.raises(SnapshotError, match="must not contain"):
        _require_serving_pin(4242)


def test_the_private_parent_must_already_be_root_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", missing)
    with pytest.raises(SnapshotError, match="missing"):
        publish_snapshot._open_restore_parent()
    present = tmp_path / "restore"
    present.mkdir()
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", present)
    monkeypatch.setattr(os, "lstat", lambda _path: _Stat(stat.S_IFDIR | 0o755, 0))
    with pytest.raises(SnapshotError, match="root-only"):
        publish_snapshot._open_restore_parent()


def test_a_private_directory_that_is_not_root_only_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    serving_host: dict[str, Path],
    no_database: None,
) -> None:
    _publish(serving_host, b"PGDMP-owner")
    current = os.fstat

    def fstat(fd: int) -> os.stat_result | _Stat:
        info = current(fd)
        if stat.S_ISDIR(info.st_mode):
            return _Stat(stat.S_IFDIR | 0o700, 1000, dev=info.st_dev, ino=info.st_ino)
        return info

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(SnapshotError, match="root-only"):
        restore_accepted("staging", RELEASE)
    assert list(serving_host["restore"].iterdir()) == []


def test_cli_accepts_only_the_cluster_and_the_release_id() -> None:
    with pytest.raises(SystemExit):
        main(["restore-accepted", "staging"])
    with pytest.raises(SystemExit):
        main(["restore-accepted", "staging", RELEASE, "extra.dump"])
    with pytest.raises(SystemExit, match="staging cluster"):
        main(["restore-accepted", "dev", RELEASE])


def test_unsigned_staging_restore_stops_before_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database")

    monkeypatch.setattr(publish_snapshot, "_source_database", boom)
    monkeypatch.setattr(publish_snapshot, "_database_exists", boom)
    with pytest.raises(SnapshotError, match="restore-accepted"):
        restore_candidate("staging", Path("snapshot.dump"))


def test_dev_and_test_restore_still_pass_the_staging_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def source(environment: str) -> str:
        seen.append(environment)
        raise SnapshotError("stopped-after-source-check")

    monkeypatch.setattr(publish_snapshot, "_source_database", source)
    for environment in ("dev", "test"):
        with pytest.raises(SnapshotError, match="stopped-after-source-check"):
            restore_candidate(environment, Path("snapshot.dump"))
    assert seen == ["dev", "test"]


def test_staging_promotion_requires_the_authenticated_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def comment(_environment: str, _database: str) -> str:
        return comments["value"]

    comments = {"value": ""}
    monkeypatch.setattr(publish_snapshot, "_database_comment", comment)
    for value, match in (
        ("", "authenticated restore"),
        (_DEMOTED_MARK, "preserved serving generation"),
        ("openfactors-other", "authenticated restore"),
    ):
        comments["value"] = value
        with pytest.raises(SnapshotError, match=match):
            _refuse_demoted_candidate("staging")
    comments["value"] = _AUTHENTICATED_MARK
    _refuse_demoted_candidate("staging")


def test_test_promotion_still_allows_only_an_empty_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comments = {"value": ""}
    monkeypatch.setattr(
        publish_snapshot,
        "_database_comment",
        lambda _environment, _database: comments["value"],
    )
    _refuse_demoted_candidate("test")
    comments["value"] = _AUTHENTICATED_MARK
    with pytest.raises(SnapshotError, match="unexpected catalog comment"):
        _refuse_demoted_candidate("test")
    comments["value"] = _DEMOTED_MARK
    with pytest.raises(SnapshotError, match="preserved serving generation"):
        _refuse_demoted_candidate("test")


def test_cleanup_refuses_an_unmarked_or_authenticated_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {
        CANDIDATE_DATABASE: "open",
        SERVING_DATABASE: "open",
        PREVIOUS_DATABASE: "open",
        "openfactors_expired": None,
    }
    monkeypatch.setattr(publish_snapshot, "_cutover_catalog", lambda _environment: catalog)
    monkeypatch.setattr(publish_snapshot, "_target_connections", lambda _environment, _target: 0)
    for comment in ("", _AUTHENTICATED_MARK):
        monkeypatch.setattr(
            publish_snapshot,
            "_database_comment",
            lambda _environment, _database, comment=comment: comment,
        )
        report = _cleanup_report("staging", CANDIDATE_DATABASE, RELEASE)
        assert report["allowed"] is False
        assert report["dropped"] is False


def test_create_candidate_requires_the_generation_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database")

    monkeypatch.setattr(publish_snapshot, "_admin", boom)
    with pytest.raises(SnapshotError, match="lock was lost"):
        _create_candidate("staging")


def test_the_comment_is_written_only_after_validation_while_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    dump = tmp_path / f"{RELEASE}.dump"
    dump.write_bytes(b"PGDMP")
    manifest = tmp_path / f"{RELEASE}.json"
    manifest.write_bytes(_manifest_bytes(RELEASE, b"PGDMP"))
    private = publish_snapshot._PrivateCopy(tmp_path, RELEASE, hashlib.sha256(b"PGDMP").hexdigest())

    class Held:
        environment = "staging"

        def __enter__(self) -> Held:
            self.token = _active_generation_lock.set(self)
            return self

        def __exit__(self, *_exc: object) -> bool:
            _active_generation_lock.reset(self.token)
            return False

        def session_holds_lock(self) -> bool:
            order.append("held")
            return True

    def admin(_environment: str, _database: str, sql: str) -> str:
        order.append(sql)
        if "shobj_description" in sql:
            return _AUTHENTICATED_MARK
        return ""

    monkeypatch.setattr(publish_snapshot, "_generation_lock", lambda _environment: Held())
    monkeypatch.setattr(publish_snapshot, "_database_exists", lambda *_args: False)
    monkeypatch.setattr(publish_snapshot, "_copy_to_container", lambda *_args: None)
    monkeypatch.setattr(publish_snapshot, "_remove_container_dump", lambda *_args: None)
    monkeypatch.setattr(
        publish_snapshot,
        "_pg_restore",
        lambda *_args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        publish_snapshot,
        "validate_candidate",
        lambda *_args: order.append("validate") or {"candidate_database": CANDIDATE_DATABASE},
    )
    monkeypatch.setattr(publish_snapshot, "assert_cluster_roles", lambda *_args: None)
    monkeypatch.setattr(publish_snapshot, "_admin", admin)
    _restore_private_candidate("staging", private)
    validate_at = order.index("validate")
    comment_at = next(index for index, item in enumerate(order) if "COMMENT ON DATABASE" in item)
    assert validate_at < comment_at
    assert f"IS '{_AUTHENTICATED_MARK}'" in order[comment_at]
    assert all("DROP DATABASE" not in item for item in order)


def test_a_lost_lock_does_not_write_the_comment_or_drop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dump = tmp_path / f"{RELEASE}.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / f"{RELEASE}.json").write_bytes(_manifest_bytes(RELEASE, b"PGDMP"))
    private = publish_snapshot._PrivateCopy(tmp_path, RELEASE, hashlib.sha256(b"PGDMP").hexdigest())
    holds = {"count": 0}
    sqls: list[str] = []

    class Flaky:
        environment = "staging"

        def __enter__(self) -> Flaky:
            self.token = _active_generation_lock.set(self)
            return self

        def __exit__(self, *_exc: object) -> bool:
            _active_generation_lock.reset(self.token)
            return False

        def session_holds_lock(self) -> bool:
            holds["count"] += 1
            return holds["count"] <= 2

    def admin(_environment: str, _database: str, sql: str) -> str:
        sqls.append(sql)
        if sql.startswith("DROP") or "COMMENT ON" in sql or "pg_terminate" in sql:
            raise AssertionError(sql)
        return ""

    monkeypatch.setattr(publish_snapshot, "_generation_lock", lambda _environment: Flaky())
    monkeypatch.setattr(publish_snapshot, "_database_exists", lambda *_args: False)
    monkeypatch.setattr(publish_snapshot, "_copy_to_container", lambda *_args: None)
    monkeypatch.setattr(publish_snapshot, "_remove_container_dump", lambda *_args: None)
    monkeypatch.setattr(
        publish_snapshot,
        "_pg_restore",
        lambda *_args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        publish_snapshot,
        "validate_candidate",
        lambda *_args: {"candidate_database": CANDIDATE_DATABASE},
    )
    monkeypatch.setattr(publish_snapshot, "assert_cluster_roles", lambda *_args: None)
    monkeypatch.setattr(publish_snapshot, "_admin", admin)
    with pytest.raises(SnapshotError, match="lock was lost"):
        _restore_private_candidate("staging", private)
    assert any(sql.startswith("CREATE DATABASE openfactors_candidate") for sql in sqls)
    assert not any("DROP DATABASE" in sql or "COMMENT ON" in sql for sql in sqls)


def test_a_failed_comment_drops_only_the_candidate_this_command_created(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dump = tmp_path / f"{RELEASE}.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / f"{RELEASE}.json").write_bytes(_manifest_bytes(RELEASE, b"PGDMP"))
    private = publish_snapshot._PrivateCopy(tmp_path, RELEASE, hashlib.sha256(b"PGDMP").hexdigest())
    dropped: list[str] = []

    class Held:
        environment = "staging"

        def __enter__(self) -> Held:
            self.token = _active_generation_lock.set(self)
            return self

        def __exit__(self, *_exc: object) -> bool:
            _active_generation_lock.reset(self.token)
            return False

        def session_holds_lock(self) -> bool:
            return True

    def admin(_environment: str, _database: str, sql: str) -> str:
        if "COMMENT ON" in sql:
            raise SnapshotError("comment failed")
        return ""

    monkeypatch.setattr(publish_snapshot, "_generation_lock", lambda _environment: Held())
    monkeypatch.setattr(publish_snapshot, "_database_exists", lambda *_args: False)
    monkeypatch.setattr(publish_snapshot, "_copy_to_container", lambda *_args: None)
    monkeypatch.setattr(publish_snapshot, "_remove_container_dump", lambda *_args: None)
    monkeypatch.setattr(
        publish_snapshot,
        "_pg_restore",
        lambda *_args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        publish_snapshot,
        "validate_candidate",
        lambda *_args: {"candidate_database": CANDIDATE_DATABASE},
    )
    monkeypatch.setattr(publish_snapshot, "assert_cluster_roles", lambda *_args: None)
    monkeypatch.setattr(publish_snapshot, "_admin", admin)
    monkeypatch.setattr(
        publish_snapshot,
        "drop_database",
        lambda _environment, name: dropped.append(name),
    )
    with pytest.raises(SnapshotError, match="comment failed"):
        _restore_private_candidate("staging", private)
    assert dropped == [CANDIDATE_DATABASE]


def test_an_existing_candidate_is_left_in_place(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dump = tmp_path / f"{RELEASE}.dump"
    dump.write_bytes(b"PGDMP")
    (tmp_path / f"{RELEASE}.json").write_bytes(_manifest_bytes(RELEASE, b"PGDMP"))
    private = publish_snapshot._PrivateCopy(tmp_path, RELEASE, hashlib.sha256(b"PGDMP").hexdigest())

    class Held:
        environment = "staging"

        def __enter__(self) -> Held:
            self.token = _active_generation_lock.set(self)
            return self

        def __exit__(self, *_exc: object) -> bool:
            _active_generation_lock.reset(self.token)
            return False

        def session_holds_lock(self) -> bool:
            return True

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database")

    monkeypatch.setattr(publish_snapshot, "_generation_lock", lambda _environment: Held())
    monkeypatch.setattr(publish_snapshot, "_database_exists", lambda *_args: True)
    monkeypatch.setattr(publish_snapshot, "_admin", boom)
    monkeypatch.setattr(publish_snapshot, "drop_database", boom)
    monkeypatch.setattr(publish_snapshot, "_create_candidate", boom)
    monkeypatch.setattr(publish_snapshot, "assert_cluster_roles", lambda *_args: None)
    monkeypatch.setattr(publish_snapshot, "_remove_container_dump", lambda *_args: None)
    with pytest.raises(SnapshotError, match="already exists"):
        _restore_private_candidate("staging", private)


def test_a_lost_lock_refuses_the_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database")

    class Lost:
        environment = "staging"

        def session_holds_lock(self) -> bool:
            return False

    monkeypatch.setattr(publish_snapshot, "_admin", boom)
    token = _active_generation_lock.set(Lost())  # type: ignore[arg-type]
    try:
        with pytest.raises(SnapshotError, match="lock was lost"):
            _mark_authenticated_candidate("staging")
    finally:
        _active_generation_lock.reset(token)


def test_private_cleanup_does_not_follow_a_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "restore"
    root.mkdir()
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", root)
    child = root / f"{RELEASE}.1.token"
    child.mkdir()
    kept = tmp_path / "kept.txt"
    kept.write_text("keep", encoding="utf-8")
    nested = child / "READY"
    try:
        os.symlink(kept, nested)
    except OSError:
        nested.mkdir()
        kept = nested / "secret.txt"
        kept.write_text("keep", encoding="utf-8")
    private = publish_snapshot._PrivateCopy(child, RELEASE, "")
    publish_snapshot._discard_private_restore(private)
    assert kept.read_text(encoding="utf-8") == "keep"


def test_recovery_docs_do_not_delete_the_restore_parent_or_accepted_snapshots() -> None:
    text = Path("docs/decisions/snapshot-transfer-acceptance.md").read_text(encoding="utf-8")
    assert "deletes only that one child" in text
    assert "Do not delete the parent directory" in text
    assert "do not delete `accepted/`" in text
