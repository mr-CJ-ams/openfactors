"""Accept a signed snapshot upload without opening a database."""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops import accept_transfer, publish_snapshot
from ops.accept_transfer import (
    COMMIT_NAME,
    MAX_COMMIT_BYTES,
    MAX_DUMP_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SIGNATURE_BYTES,
    READY_NAME,
    TransferAcceptError,
    accept_release,
    assert_uploader_is_not_acceptor,
    main,
    publish_without_replace,
)
from ops.publish_snapshot import (
    DATASET_ID,
    EXCLUDED_SCHEMAS,
    EXCLUDED_TABLES,
    SIGNATURE_NAMESPACE,
    SIGNATURE_PRINCIPAL,
    verify_manifest,
)

RELEASE = "11111111-1111-4111-8111-111111111111"
_MARKER = f'namespaces="{SIGNATURE_NAMESPACE}"'
_FORBIDDEN = (
    "restore_candidate",
    "promote_candidate",
    "rollback_serving",
    "cleanup_generation",
    "psycopg",
    "DROP DATABASE",
    "os.rename",
)


@dataclass(frozen=True)
class Upload:
    release_id: str
    incoming: Path
    accept_root: Path
    signers: Path
    directory: Path
    dump: bytes


@pytest.fixture(autouse=True)
def _acceptance_is_not_the_uploader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 65_534, raising=False)


@pytest.fixture(autouse=True)
def _database_commands_are_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "create_snapshot",
        "restore_candidate",
        "abort_candidate",
        "promote_candidate",
        "rollback_serving",
        "cleanup_generation",
        "_admin",
        "_run",
        "_dump",
    ):
        monkeypatch.setattr(publish_snapshot, name, _forbidden(name))


@pytest.fixture
def publication(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "linux":
        return

    def noreplace(source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)
        os.rename(source, destination)

    monkeypatch.setattr(accept_transfer, "publish_without_replace", noreplace)


@pytest.fixture(scope="module")
def publisher_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _key(tmp_path_factory.mktemp("publisher"), "publisher")


def test_a_signed_upload_is_accepted(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    seen: list[str] = []
    real = accept_transfer.verify_manifest

    def spy(manifest: Path, signature: Path, signers: Path) -> None:
        seen.append(signature.name)
        real(manifest, signature, signers)

    monkeypatch.setattr(accept_transfer, "verify_manifest", spy)
    accepted = accept_release(
        upload.release_id, upload.incoming, upload.accept_root, upload.signers
    )
    ready = accepted / READY_NAME
    assert accepted == upload.accept_root / "accepted" / RELEASE
    assert seen == [f"{RELEASE}.json.sig"]
    assert (accepted / f"{RELEASE}.dump").read_bytes() == upload.dump
    assert ready.read_bytes() == f"openfactors-accepted\n{RELEASE}\n".encode()
    assert b"BEGIN SSH SIGNATURE" not in ready.read_bytes()
    assert COMMIT_NAME not in {path.name for path in accepted.iterdir()}
    assert not list((upload.accept_root / "staging").glob(f"{RELEASE}.*"))
    if os.name == "posix":
        assert accepted.stat().st_mode & 0o777 == 0o550
        assert (accepted / f"{RELEASE}.dump").stat().st_mode & 0o777 == 0o440


def test_the_same_bytes_are_idempotent(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    first = accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    dump = first / f"{RELEASE}.dump"
    before = dump.read_bytes()
    inode = dump.stat().st_ino
    second = accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert second == first
    assert dump.read_bytes() == before
    assert dump.stat().st_ino == inode


def test_different_bytes_leave_the_accepted_release(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key, dump=b"first-dump")
    accepted = accept_release(
        upload.release_id, upload.incoming, upload.accept_root, upload.signers
    )
    original = (accepted / f"{RELEASE}.dump").read_bytes()
    _replace_upload(upload, publisher_key, dump=b"other-dump")
    with pytest.raises(TransferAcceptError, match="different bytes"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert (accepted / f"{RELEASE}.dump").read_bytes() == original


def test_a_changed_manifest_byte_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    manifest = upload.directory / f"{RELEASE}.json.partial"
    updated = manifest.read_bytes().replace(b"openfactors_dev", b"openfactors_xyz")
    assert len(updated) == len(manifest.read_bytes())
    manifest.write_bytes(updated)
    _rewrite_commit(upload.directory, upload.release_id)
    _stamp(upload.directory, upload.release_id)
    with pytest.raises(TransferAcceptError, match="rejected|does not describe|checksum|marker"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert not (upload.accept_root / "accepted" / RELEASE).exists()


def test_a_changed_dump_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key, dump=b"original-dump")
    dump = upload.directory / f"{RELEASE}.dump.partial"
    dump.write_bytes(b"tampered-dump")
    _rewrite_commit(upload.directory, upload.release_id)
    _stamp(upload.directory, upload.release_id)
    with pytest.raises(TransferAcceptError, match="checksum"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert not (upload.accept_root / "accepted" / RELEASE).exists()


def test_a_wrong_signing_key_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    other = _key(tmp_path, "other")
    _signers(tmp_path, other).replace(upload.signers)
    with pytest.raises(TransferAcceptError, match="rejected"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_a_missing_file_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    (upload.directory / COMMIT_NAME).unlink()
    with pytest.raises(TransferAcceptError, match="missing"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_an_unexpected_file_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    (upload.directory / "notes.txt").write_text("no\n", encoding="utf-8")
    with pytest.raises(TransferAcceptError, match="unexpected"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_a_symlink_reported_by_the_directory_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject_reported_symlink(_upload(tmp_path, publisher_key), monkeypatch)


def test_a_symlink_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    target = upload.directory / f"{RELEASE}.dump.partial"
    payload = target.read_bytes()
    target.unlink()
    outside = tmp_path / "outside.dump"
    outside.write_bytes(payload)
    try:
        target.symlink_to(outside)
    except OSError:
        _reject_reported_symlink(upload, monkeypatch)
        return
    with pytest.raises(TransferAcceptError, match="symlink"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_a_path_with_dotdot_is_rejected(tmp_path: Path, publisher_key: Path) -> None:
    upload = _upload(tmp_path, publisher_key)
    with pytest.raises(TransferAcceptError, match="canonical"):
        accept_release(
            f"../{RELEASE}",
            upload.incoming,
            upload.accept_root,
            upload.signers,
        )


def test_an_uppercase_uuid_is_rejected(tmp_path: Path, publisher_key: Path) -> None:
    upload = _upload(tmp_path, publisher_key)
    with pytest.raises(TransferAcceptError, match="canonical"):
        accept_release(
            "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
            upload.incoming,
            upload.accept_root,
            upload.signers,
        )


def test_a_non_uuid_directory_is_rejected(tmp_path: Path, publisher_key: Path) -> None:
    upload = _upload(tmp_path, publisher_key)
    with pytest.raises(TransferAcceptError, match="canonical"):
        accept_release("release-1", upload.incoming, upload.accept_root, upload.signers)


def test_an_oversized_file_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(accept_transfer, "MAX_DUMP_BYTES", 8)
    upload = _upload(tmp_path, publisher_key, dump=b"0123456789")
    with pytest.raises(TransferAcceptError, match="larger than the acceptance limit"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert not (upload.accept_root / "accepted" / RELEASE).exists()


def test_the_size_guards_match_the_decision() -> None:
    assert (MAX_DUMP_BYTES, MAX_MANIFEST_BYTES, MAX_SIGNATURE_BYTES, MAX_COMMIT_BYTES) == (
        32_000_000,
        65_536,
        16_384,
        4_096,
    )


def test_in_place_modification_is_not_accepted_from_size_alone(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key, dump=b"same-size")
    dump = upload.directory / f"{RELEASE}.dump.partial"
    before = dump.stat()
    handle = os.open(dump, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        assert os.write(handle, b"SAME-SIZE") == len(b"SAME-SIZE")
    finally:
        os.close(handle)
    after = dump.stat()
    assert before.st_ino == after.st_ino
    assert before.st_size == after.st_size
    _stamp(upload.directory, upload.release_id)
    with pytest.raises(TransferAcceptError, match="does not match the copied bytes"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_a_file_that_changes_while_it_is_read_is_refused(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _upload(tmp_path, publisher_key, dump=b"stable-dump")
    dump = upload.directory / f"{RELEASE}.dump.partial"
    original = os.read
    flipped = False

    def wrapper(handle: int, size: int) -> bytes:
        nonlocal flipped
        chunk = original(handle, size)
        if not flipped and chunk == b"stable-dump":
            flipped = True
            dump.write_bytes(b"STABLE-DUMP")
        return chunk

    monkeypatch.setattr(os, "read", wrapper)
    with pytest.raises(TransferAcceptError, match="changed while it was being copied"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_commit_must_be_uploaded_last(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    commit = upload.directory / COMMIT_NAME
    os.utime(commit, ns=(1_000, 1_000))
    os.utime(upload.directory / f"{RELEASE}.dump.partial", ns=(5_000, 5_000))
    with pytest.raises(TransferAcceptError, match="uploaded last"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_a_directory_without_ready_is_not_accepted(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    accepted = upload.accept_root / "accepted" / RELEASE
    accepted.mkdir(parents=True)
    (accepted / f"{RELEASE}.dump").write_bytes(upload.dump)
    (accepted / f"{RELEASE}.json").write_bytes(
        (upload.directory / f"{RELEASE}.json.partial").read_bytes()
    )
    signature = (upload.directory / f"{RELEASE}.json.sig.partial").read_bytes()
    (accepted / f"{RELEASE}.json.sig").write_bytes(signature)
    with pytest.raises(TransferAcceptError, match="without READY"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert not (accepted / READY_NAME).exists()
    assert (accepted / f"{RELEASE}.dump").read_bytes() == upload.dump


def test_a_second_acceptance_is_refused_while_the_lock_is_held(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}
    original = accept_transfer._read_verified

    def slow(path: Path, limit: int, **kwargs: object) -> tuple[bytes, os.stat_result]:
        if path.name.endswith(".dump.partial") and not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(path, limit, **kwargs)

    monkeypatch.setattr(accept_transfer, "_read_verified", slow)

    def runner() -> None:
        try:
            outcome["path"] = accept_release(
                upload.release_id,
                upload.incoming,
                upload.accept_root,
                upload.signers,
            )
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=runner)
    worker.start()
    assert entered.wait(10)
    with pytest.raises(TransferAcceptError, match="Another acceptance"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert "error" not in outcome
    assert outcome["path"] == upload.accept_root / "accepted" / RELEASE


def test_a_publication_failure_leaves_no_accepted_release(
    tmp_path: Path,
    publisher_key: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _upload(tmp_path, publisher_key)

    def fail(_source: Path, _destination: Path) -> None:
        raise OSError("injected")

    monkeypatch.setattr(accept_transfer, "publish_without_replace", fail)
    with pytest.raises(TransferAcceptError, match="could not be published"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert not (upload.accept_root / "accepted" / RELEASE).exists()
    assert list((upload.accept_root / "staging").glob("*")) == []


def test_publication_does_not_replace_an_existing_directory(tmp_path: Path) -> None:
    source = tmp_path / "staging"
    source.mkdir()
    (source / READY_NAME).write_bytes(b"openfactors-accepted\n")
    destination = tmp_path / "accepted"
    destination.mkdir()
    (destination / READY_NAME).write_bytes(b"keep\n")
    if sys.platform == "linux":
        with pytest.raises(FileExistsError):
            publish_without_replace(source, destination)
        assert (destination / READY_NAME).read_bytes() == b"keep\n"
        assert source.is_dir()
        return
    with pytest.raises(TransferAcceptError, match="renameat2"):
        publish_without_replace(source, destination)
    assert (destination / READY_NAME).read_bytes() == b"keep\n"


def test_the_source_does_not_call_database_commands() -> None:
    source = inspect.getsource(accept_transfer)
    for name in _FORBIDDEN:
        assert name not in source
    assert "renameat2" in inspect.getsource(publish_without_replace)
    assert "os.rename" not in inspect.getsource(publish_without_replace)


def test_stale_staging_is_discarded(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    stale = upload.accept_root / "staging" / f"{RELEASE}.old"
    stale.mkdir(parents=True)
    (stale / "partial").write_text("left\n", encoding="utf-8")
    accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert not stale.exists()


def test_a_writable_parent_cannot_hold_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = tmp_path / "accept" / "allowed_signers"
    with pytest.raises(TransferAcceptError, match="can replace"):
        _check_pin(
            monkeypatch,
            pin,
            {
                pin: _mode(stat.S_IFREG | 0o440, 0, 50, 1),
                pin.parent: _mode(stat.S_IFDIR | 0o750, 50, 50, 2),
            },
        )


def test_a_writable_ancestor_cannot_hold_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = tmp_path / "home" / "pin" / "allowed_signers"
    with pytest.raises(TransferAcceptError, match="can replace"):
        _check_pin(
            monkeypatch,
            pin,
            {
                pin: _mode(stat.S_IFREG | 0o440, 0, 50, 1),
                pin.parent: _mode(stat.S_IFDIR | 0o750, 0, 50, 2),
                pin.parent.parent: _mode(stat.S_IFDIR | 0o750, 50, 50, 3),
            },
        )


def test_a_writable_pin_file_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = tmp_path / "pin" / "allowed_signers"
    with pytest.raises(TransferAcceptError, match="must not be writable"):
        _check_pin(
            monkeypatch,
            pin,
            {
                pin: _mode(stat.S_IFREG | 0o644, 50, 50, 1),
                pin.parent: _mode(stat.S_IFDIR | 0o755, 0, 0, 2),
            },
        )


def test_a_symlink_pin_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pin = tmp_path / "pin" / "allowed_signers"
    with pytest.raises(TransferAcceptError, match="regular file"):
        _check_pin(
            monkeypatch,
            pin,
            {pin: _mode(stat.S_IFLNK | 0o777, 0, 0, 1)},
        )


def test_a_symlink_ancestor_of_the_pin_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = tmp_path / "link" / "allowed_signers"
    with pytest.raises(TransferAcceptError, match="contains a symlink"):
        _check_pin(
            monkeypatch,
            pin,
            {
                pin: _mode(stat.S_IFREG | 0o440, 0, 50, 1),
                pin.parent: _mode(stat.S_IFLNK | 0o777, 0, 0, 2),
            },
        )


def test_a_root_owned_read_only_pin_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = tmp_path / "pin" / "allowed_signers"
    _check_pin(
        monkeypatch,
        pin,
        {
            pin: _mode(stat.S_IFREG | 0o440, 0, 50, 1),
            pin.parent: _mode(stat.S_IFDIR | 0o750, 0, 50, 2),
        },
    )


def test_a_sticky_directory_does_not_let_the_acceptance_user_replace_the_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = tmp_path / "pin" / "allowed_signers"
    _check_pin(
        monkeypatch,
        pin,
        {
            pin: _mode(stat.S_IFREG | 0o440, 0, 50, 1),
            pin.parent: _mode(stat.S_IFDIR | 0o1777, 0, 0, 2),
        },
    )


def test_a_pin_path_with_dotdot_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = tmp_path / "pin" / ".." / "allowed_signers"
    with pytest.raises(TransferAcceptError, match="must not contain"):
        _check_pin(monkeypatch, pin, {})


def test_an_upload_directory_symlink_is_rejected(
    tmp_path: Path,
    publisher_key: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    aside = upload.directory.with_name("real-upload")
    upload.directory.rename(aside)
    try:
        upload.directory.symlink_to(aside, target_is_directory=True)
    except OSError:
        _reject_directory_symlink(upload, monkeypatch)
        return
    with pytest.raises(TransferAcceptError, match="not a real directory"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def test_a_directory_symlink_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "O_DIRECTORY", getattr(os, "O_DIRECTORY", 0x10000), raising=False)
    monkeypatch.setattr(os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0x20000), raising=False)

    def opened(path: os.PathLike[str] | str, flags: int, *args: object, **kwargs: object) -> int:
        assert flags & os.O_DIRECTORY
        assert flags & os.O_NOFOLLOW
        raise OSError(errno.ELOOP, "too many links", os.fspath(path))

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(TransferAcceptError, match="not a real directory"):
        accept_transfer._open_directory_fd(tmp_path / "upload")


def test_replacing_the_upload_directory_does_not_supply_the_copy(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    real_entries = accept_transfer._OpenedUpload.entries

    def entries(self: accept_transfer._OpenedUpload) -> list[tuple[str, os.stat_result]]:
        if not getattr(entries, "swapped", False):
            entries.swapped = True  # type: ignore[attr-defined]
            aside = self.path.with_name("replaced-upload")
            os.rename(self.path, aside)
            self.path.mkdir()
            (self.path / "evil.partial").write_bytes(b"evil")
        return real_entries(self)

    monkeypatch.setattr(accept_transfer._OpenedUpload, "entries", entries)
    accepted = upload.accept_root / "accepted" / RELEASE
    if os.name == "posix" and getattr(os, "O_DIRECTORY", 0) and getattr(os, "O_NOFOLLOW", 0):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
        assert (accepted / f"{RELEASE}.dump").read_bytes() == upload.dump
        assert (upload.directory / "evil.partial").read_bytes() == b"evil"
        return
    with pytest.raises(TransferAcceptError, match="changed"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)
    assert not accepted.exists()


def test_a_hard_link_is_copied_as_a_new_file(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
) -> None:
    upload = _upload(tmp_path, publisher_key)
    source = upload.directory / f"{RELEASE}.dump.partial"
    alias = tmp_path / "alias.dump"
    try:
        os.link(source, alias)
    except OSError:
        alias = None
    accepted = accept_release(
        upload.release_id,
        upload.incoming,
        upload.accept_root,
        upload.signers,
    )
    published = accepted / f"{RELEASE}.dump"
    assert published.read_bytes() == upload.dump
    if alias is not None:
        assert os.lstat(published).st_ino != os.lstat(alias).st_ino


def test_the_pin_cannot_live_inside_the_upload(tmp_path: Path, publisher_key: Path) -> None:
    upload = _upload(tmp_path, publisher_key)
    planted = upload.directory / "allowed_signers"
    planted.write_bytes(upload.signers.read_bytes())
    with pytest.raises(TransferAcceptError, match="pinned outside|unexpected"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, planted)


def test_overlapping_roots_are_refused(tmp_path: Path, publisher_key: Path) -> None:
    upload = _upload(tmp_path, publisher_key)
    with pytest.raises(TransferAcceptError, match="separate"):
        accept_release(upload.release_id, upload.incoming, upload.incoming, upload.signers)


def test_the_uploader_cannot_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "upload"
    directory.mkdir()
    original = os.lstat

    def lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        info = original(path, *args, **kwargs)
        if Path(path) == directory:
            return os.stat_result((info.st_mode,) + tuple(info)[1:4] + (4242,) + tuple(info)[5:])
        return info

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "geteuid", lambda: 4242, raising=False)
    with pytest.raises(TransferAcceptError, match="different user"):
        assert_uploader_is_not_acceptor(directory)


def test_root_cannot_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "upload"
    directory.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(TransferAcceptError, match="root"):
        assert_uploader_is_not_acceptor(directory)


def test_cli_accepts_one_release(
    tmp_path: Path,
    publisher_key: Path,
    publication: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    upload = _upload(tmp_path, publisher_key)
    main(
        [
            "accept",
            upload.release_id,
            "--incoming",
            str(upload.incoming),
            "--accept-root",
            str(upload.accept_root),
            "--allowed-signers",
            str(upload.signers),
        ]
    )
    assert capsys.readouterr().out == f"Accepted {RELEASE}.\n"
    verify_manifest(
        upload.accept_root / "accepted" / RELEASE / f"{RELEASE}.json",
        upload.accept_root / "accepted" / RELEASE / f"{RELEASE}.json.sig",
        upload.signers,
    )


def _reject_reported_symlink(upload: Upload, monkeypatch: pytest.MonkeyPatch) -> None:
    def entries(self: object) -> list[tuple[str, os.stat_result]]:
        names = (
            f"{RELEASE}.dump.partial",
            f"{RELEASE}.json.partial",
            f"{RELEASE}.json.sig.partial",
            COMMIT_NAME,
        )
        found: list[tuple[str, os.stat_result]] = []
        for name in names:
            mode = stat.S_IFLNK if name.endswith(".dump.partial") else stat.S_IFREG
            found.append((name, os.stat_result((mode, 1, 2, 1, 0, 0, 1, 0, 0, 0))))
        return found

    monkeypatch.setattr(accept_transfer._OpenedUpload, "entries", entries)
    with pytest.raises(TransferAcceptError, match="symlink"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def _reject_directory_symlink(upload: Upload, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.is_symlink

    def is_symlink(self: Path) -> bool:
        if self == upload.directory:
            return True
        return real(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    with pytest.raises(TransferAcceptError, match="not a real directory"):
        accept_release(upload.release_id, upload.incoming, upload.accept_root, upload.signers)


def _mode(mode: int, uid: int, gid: int, ino: int) -> os.stat_result:
    return os.stat_result((mode, ino, 1, 1, uid, gid, 1, 0, 0, 0))


def _check_pin(
    monkeypatch: pytest.MonkeyPatch,
    pin: Path,
    nodes: dict[Path, os.stat_result],
) -> None:
    monkeypatch.setattr(accept_transfer, "_posix_credentials", lambda: (50, {50}))
    known = {Path(os.path.abspath(path)): info for path, info in nodes.items()}

    def lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        current = Path(os.path.abspath(path))
        if current in known:
            return known[current]
        return _mode(stat.S_IFDIR | 0o755, 0, 0, 9)

    monkeypatch.setattr(os, "lstat", lstat)
    accept_transfer._assert_pin_not_replaceable(pin)


def _forbidden(name: str):
    def inner(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(name)

    return inner


def _upload(tmp_path: Path, key_path: Path, *, dump: bytes = b"snapshot-dump") -> Upload:
    incoming = tmp_path / "incoming"
    accept_root = tmp_path / "accept"
    directory = incoming / RELEASE
    directory.mkdir(parents=True)
    accept_root.mkdir()
    manifest = _manifest_bytes(RELEASE, dump)
    manifest_path = directory / f"{RELEASE}.json.partial"
    manifest_path.write_bytes(manifest)
    signature = _sign(manifest_path, key_path)
    (directory / f"{RELEASE}.json.sig.partial").write_bytes(signature)
    (directory / f"{RELEASE}.dump.partial").write_bytes(dump)
    _rewrite_commit(directory, RELEASE)
    _stamp(directory, RELEASE)
    signers = _signers(tmp_path, key_path)
    return Upload(RELEASE, incoming, accept_root, signers, directory, dump)


def _replace_upload(upload: Upload, key_path: Path, *, dump: bytes) -> None:
    for child in upload.directory.iterdir():
        child.unlink()
    manifest_path = upload.directory / f"{RELEASE}.json.partial"
    manifest_path.write_bytes(_manifest_bytes(RELEASE, dump))
    (upload.directory / f"{RELEASE}.json.sig.partial").write_bytes(_sign(manifest_path, key_path))
    (upload.directory / f"{RELEASE}.dump.partial").write_bytes(dump)
    _rewrite_commit(upload.directory, RELEASE)
    _stamp(upload.directory, RELEASE)


def _rewrite_commit(directory: Path, release_id: str) -> None:
    names = (
        f"{release_id}.dump.partial",
        f"{release_id}.json.partial",
        f"{release_id}.json.sig.partial",
    )
    payload = {
        "files": [
            {
                "name": name,
                "sha256_hex": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
                "size": (directory / name).stat().st_size,
            }
            for name in names
        ],
        "marker": "openfactors-commit",
        "release_id": release_id,
    }
    (directory / COMMIT_NAME).write_bytes(
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _stamp(directory: Path, release_id: str) -> None:
    early = 1_700_000_000_000_000_000
    late = early + 5_000_000_000
    for name in (
        f"{release_id}.dump.partial",
        f"{release_id}.json.partial",
        f"{release_id}.json.sig.partial",
    ):
        os.utime(directory / name, ns=(early, early))
    os.utime(directory / COMMIT_NAME, ns=(late, late))


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


def _signers(directory: Path, key_path: Path) -> Path:
    public = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
    path = directory / "allowed_signers"
    path.write_text(f"{SIGNATURE_PRINCIPAL} {_MARKER} {public}\n", encoding="utf-8")
    return path


def _key(directory: Path, name: str) -> Path:
    path = directory / name
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
