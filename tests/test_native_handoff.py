"""Native restore handoff: modes, path checks, and cleanup."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import ops.publish_snapshot as publish_snapshot
import pytest
from ops.publish_snapshot import SnapshotError, _restore_dump_file

RELEASE = "11111111-1111-4111-8111-111111111111"
DIGEST = "a" * 64


def _info(kind: str, mode: int, *, uid: int = 0, gid: int = 80) -> SimpleNamespace:
    flag = stat.S_IFDIR if kind == "dir" else stat.S_IFREG
    return SimpleNamespace(st_mode=flag | mode, st_uid=uid, st_gid=gid)


def test_handoff_modes_accept_only_the_limited_set() -> None:
    publish_snapshot._require_handoff_modes(
        parent_mode=0o710,
        directory_mode=0o550,
        file_mode=0o440,
        parent_uid=0,
        directory_uid=0,
        file_uid=0,
        parent_gid=80,
        directory_gid=80,
        file_gid=80,
        expected_gid=80,
    )
    rejected = (
        (0o777, 0o550, 0o440, 0, 80),
        (0o710, 0o775, 0o440, 0, 80),
        (0o710, 0o550, 0o666, 0, 80),
        (0o755, 0o550, 0o440, 0, 80),
        (0o710, 0o755, 0o440, 0, 80),
        (0o710, 0o550, 0o644, 0, 80),
        (0o700, 0o550, 0o440, 0, 80),
        (0o710, 0o700, 0o440, 0, 80),
        (0o710, 0o550, 0o600, 0, 80),
        (0o710, 0o550, 0o440, 1, 80),
        (0o710, 0o550, 0o440, 0, 99),
    )
    for parent, directory, file_mode, uid, gid in rejected:
        with pytest.raises(SnapshotError, match="handoff"):
            publish_snapshot._require_handoff_modes(
                parent_mode=parent,
                directory_mode=directory,
                file_mode=file_mode,
                parent_uid=uid,
                directory_uid=uid,
                file_uid=uid,
                parent_gid=gid,
                directory_gid=gid,
                file_gid=gid,
                expected_gid=80,
            )


def test_private_copy_parent_still_accepts_mode_0700() -> None:
    publish_snapshot._require_restore_parent(_info("dir", 0o700), native_handoff=False)


def test_native_parent_requires_mode_0710(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish_snapshot, "_of_restore_gid", lambda: 80)

    def lstat(path: Path) -> SimpleNamespace:
        assert Path(path) == publish_snapshot.RESTORE_ROOT
        return _info("dir", 0o710, gid=80)

    monkeypatch.setattr(os, "lstat", lstat)
    publish_snapshot._require_native_restore_parent()

    def too_private(path: Path) -> SimpleNamespace:
        return _info("dir", 0o700, gid=0)

    monkeypatch.setattr(os, "lstat", too_private)
    with pytest.raises(SnapshotError, match="0710"):
        publish_snapshot._require_native_restore_parent()

    info = _info("dir", 0o755, gid=0)
    with pytest.raises(SnapshotError, match="root-only"):
        publish_snapshot._require_restore_parent(info, native_handoff=False)


def test_private_dump_source_rejects_unsafe_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "restore"
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", root)
    source = root / f"{RELEASE}.9.abc" / f"{RELEASE}.dump"
    assert publish_snapshot._assert_private_dump_source(source) == RELEASE
    outside = tmp_path / "accepted" / RELEASE / f"{RELEASE}.dump"
    with pytest.raises(SnapshotError, match="private copy"):
        publish_snapshot._assert_private_dump_source(outside)
    accepted = Path("/var/openfactors/accept/accepted") / RELEASE / f"{RELEASE}.dump"
    with pytest.raises(SnapshotError):
        publish_snapshot._assert_private_dump_source(accepted)
    with pytest.raises(SnapshotError, match="not absolute"):
        publish_snapshot._assert_private_dump_source(Path(f"{RELEASE}.dump"))
    with pytest.raises(SnapshotError, match="leaves the private"):
        publish_snapshot._assert_private_dump_source(root / ".." / f"{RELEASE}.dump")


def test_restore_argument_rejects_accepted_and_unhanded_paths() -> None:
    handoff = f"/var/openfactors/restore/sample.handoff/{RELEASE}.dump"
    publish_snapshot._reject_native_restore_argument(handoff, harness=False)
    publish_snapshot._reject_native_restore_argument("/tmp/sample.dump", harness=True)
    with pytest.raises(SnapshotError, match="accepted snapshot"):
        publish_snapshot._reject_native_restore_argument(
            f"/var/openfactors/accept/accepted/{RELEASE}/{RELEASE}.dump",
            harness=False,
        )
    with pytest.raises(SnapshotError, match="accepted snapshot"):
        publish_snapshot._reject_native_restore_argument(
            f"/var/openfactors/accept/accepted/{RELEASE}/{RELEASE}.dump",
            harness=True,
        )
    with pytest.raises(SnapshotError, match="leaves the private"):
        publish_snapshot._reject_native_restore_argument(
            f"/var/openfactors/restore/{RELEASE}.9.abc/{RELEASE}.dump",
            harness=False,
        )
    with pytest.raises(SnapshotError, match="not absolute"):
        publish_snapshot._reject_native_restore_argument(
            "/var/openfactors/restore/sample.handoff/../../etc/passwd",
            harness=False,
        )
    with pytest.raises(SnapshotError, match="not absolute"):
        publish_snapshot._reject_native_restore_argument("relative.dump", harness=False)


def test_discard_removes_one_dump_and_keeps_neighbors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "restore"
    root.mkdir()
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", root)
    kept = root / "kept.dump"
    kept.write_bytes(b"keep")
    handoff = root / "sample.handoff"
    handoff.mkdir()
    (handoff / f"{RELEASE}.dump").write_bytes(b"copy")
    publish_snapshot._discard_native_handoff(handoff)
    assert not handoff.exists()
    assert kept.read_bytes() == b"keep"

    empty = root / "empty.handoff"
    empty.mkdir()
    publish_snapshot._discard_native_handoff(empty)
    assert not empty.exists()


def test_discard_leaves_unexpected_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "restore"
    root.mkdir()
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", root)
    handoff = root / "bad.handoff"
    handoff.mkdir()
    (handoff / f"{RELEASE}.dump").write_bytes(b"copy")
    (handoff / "extra.txt").write_bytes(b"extra")
    with pytest.raises(SnapshotError, match="not safe"):
        publish_snapshot._discard_native_handoff(handoff)
    assert (handoff / "extra.txt").read_bytes() == b"extra"

    nested_root = root / "nested.handoff"
    nested_root.mkdir()
    secret = nested_root / "nested" / "secret"
    secret.parent.mkdir()
    secret.write_bytes(b"secret")
    with pytest.raises(SnapshotError, match="not safe"):
        publish_snapshot._discard_native_handoff(nested_root)
    assert secret.read_bytes() == b"secret"

    outside = tmp_path / "outside.handoff"
    outside.mkdir()
    (outside / f"{RELEASE}.dump").write_bytes(b"stay")
    with pytest.raises(SnapshotError, match="not safe"):
        publish_snapshot._discard_native_handoff(outside)
    assert (outside / f"{RELEASE}.dump").read_bytes() == b"stay"

    link = root / "linked.handoff"
    try:
        os.symlink(tmp_path, link, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(SnapshotError, match="not safe"):
        publish_snapshot._discard_native_handoff(link)
    assert (tmp_path / "outside.handoff").is_dir()


def test_failed_pg_restore_removes_the_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "restore"
    root.mkdir()
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", root)
    monkeypatch.setenv("OPENFACTORS_DB_BACKEND", "native")
    monkeypatch.delenv("OPENFACTORS_NATIVE_TEST_HARNESS", raising=False)
    source = root / f"{RELEASE}.9.abc" / f"{RELEASE}.dump"
    source.parent.mkdir()
    source.write_bytes(b"verified-dump")
    handoff = root / f"{source.parent.name}.handoff"
    staged = handoff / f"{RELEASE}.dump"

    def stage(path: Path, digest: str) -> Path:
        assert path == source
        assert digest == DIGEST
        handoff.mkdir()
        staged.write_bytes(b"verified-dump")
        return staged

    def fail(path: str) -> None:
        assert path == staged.as_posix()
        assert ".handoff" in path
        assert "/var/openfactors/accept" not in path
        assert source.as_posix() not in path
        raise SnapshotError("boom")

    monkeypatch.setattr(publish_snapshot, "_stage_native_handoff", stage)
    monkeypatch.setattr(publish_snapshot, "_reject_native_restore_argument", lambda *_a, **_k: None)
    monkeypatch.setattr(publish_snapshot, "_run_native_pg_restore", fail)
    with pytest.raises(SnapshotError, match="boom"):
        _restore_dump_file("serving", source, expected_digest=DIGEST)
    assert not handoff.exists()
    assert source.read_bytes() == b"verified-dump"


def test_successful_restore_passes_the_handoff_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "restore"
    root.mkdir()
    monkeypatch.setattr(publish_snapshot, "RESTORE_ROOT", root)
    monkeypatch.setenv("OPENFACTORS_DB_BACKEND", "native")
    monkeypatch.delenv("OPENFACTORS_NATIVE_TEST_HARNESS", raising=False)
    source = root / f"{RELEASE}.9.abc" / f"{RELEASE}.dump"
    source.parent.mkdir()
    source.write_bytes(b"verified-dump")
    handoff = root / f"{source.parent.name}.handoff"
    staged = handoff / f"{RELEASE}.dump"
    seen: list[str] = []

    def stage(path: Path, digest: str) -> Path:
        handoff.mkdir()
        staged.write_bytes(path.read_bytes())
        assert digest == DIGEST
        return staged

    monkeypatch.setattr(publish_snapshot, "_stage_native_handoff", stage)
    monkeypatch.setattr(
        publish_snapshot,
        "_reject_native_restore_argument",
        lambda path, **_k: seen.append(path),
    )
    monkeypatch.setattr(publish_snapshot, "_run_native_pg_restore", lambda path: seen.append(path))
    monkeypatch.setattr(publish_snapshot, "_apply_native_privileges", lambda _environment: None)
    _restore_dump_file("serving", source, expected_digest=DIGEST)
    assert seen == [staged.as_posix(), staged.as_posix()]
    assert all(".handoff" in path for path in seen)
    assert all("/var/openfactors/accept" not in path for path in seen)
    assert all(source.as_posix() not in path for path in seen)
    assert not handoff.exists()
    assert source.read_bytes() == b"verified-dump"


def test_compose_restore_does_not_stage_a_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENFACTORS_DB_BACKEND", raising=False)
    monkeypatch.delenv("OPENFACTORS_NATIVE_TEST_HARNESS", raising=False)

    def forbidden(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("Compose staged a native handoff.")

    monkeypatch.setattr(publish_snapshot, "_stage_native_handoff", forbidden)
    monkeypatch.setattr(publish_snapshot, "_copy_to_container", lambda *_a, **_k: None)
    monkeypatch.setattr(
        publish_snapshot,
        "_pg_restore",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(publish_snapshot, "_remove_container_dump", lambda *_a, **_k: None)
    _restore_dump_file("staging", tmp_path / "sample.dump")


def test_native_restore_without_a_digest_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENFACTORS_DB_BACKEND", "native")
    monkeypatch.delenv("OPENFACTORS_NATIVE_TEST_HARNESS", raising=False)
    with pytest.raises(SnapshotError, match="checksum"):
        _restore_dump_file("serving", Path("/tmp/sample.dump"))
