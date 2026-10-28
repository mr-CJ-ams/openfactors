"""Accept one signed snapshot upload into a private directory.

The SFTP login can write four partial files. A different local user copies
those bytes, checks the completion marker, and verifies the publisher
signature. Publication uses Linux renameat2(RENAME_NOREPLACE). This command
does not connect to PostgreSQL. Public Beta v0.1 stays no-go.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from ops.publish_snapshot import SnapshotError, load_manifest, verify_dump, verify_manifest

MAX_DUMP_BYTES = 32_000_000
MAX_MANIFEST_BYTES = 65_536
MAX_SIGNATURE_BYTES = 16_384
MAX_COMMIT_BYTES = 4_096
COMMIT_NAME = "COMMIT.partial"
READY_NAME = "READY"
_COMMIT_MARKER = "openfactors-commit"
_READY_PREFIX = "openfactors-accepted\n"
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class TransferAcceptError(RuntimeError):
    """An upload was refused. No accepted release was replaced."""


def accept_release(
    release_id: str,
    incoming: Path,
    accept_root: Path,
    allowed_signers: Path,
) -> Path:
    """Copy one upload into accept_root/accepted/<release_id>.

    An identical accepted release is a success. Different bytes are left in
    place. The completion marker is not a signature, and READY is not one
    either.
    """
    release_id = _canonical_uuid(release_id)
    incoming = _real_directory(Path(incoming), "transfer")
    accept_root = _real_directory(Path(accept_root), "acceptance")
    signers = Path(allowed_signers)
    _separate(incoming, accept_root)
    upload = _upload_directory(incoming, release_id)
    assert_uploader_is_not_acceptor(upload)
    _mkdir_mode(accept_root / "locks", 0o700)
    with _exclusive_lock(accept_root / "locks" / f"{release_id}.lock"):
        _prepare_accept_root(accept_root)
        _require_pin(signers, incoming, accept_root)
        return _accept_locked(release_id, upload, accept_root, signers)


def assert_uploader_is_not_acceptor(upload_directory: Path) -> None:
    """Refuse the SFTP account, and refuse root.

    Windows has no uid for this check. Publication still requires Linux.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return
    if geteuid() == 0:
        raise TransferAcceptError("Acceptance must not run as root.")
    owner = os.lstat(upload_directory).st_uid
    if owner == geteuid():
        raise TransferAcceptError("Acceptance must run as a different user from the SFTP uploader.")


def publish_without_replace(source: Path, destination: Path) -> None:
    """Publish a directory with Linux renameat2(RENAME_NOREPLACE).

    There is no fallback that can replace an existing file. A host that is
    not Linux refuses to publish.
    """
    if sys.platform != "linux":
        raise TransferAcceptError(
            "Accepted releases are published only with Linux renameat2(RENAME_NOREPLACE)."
        )
    _renameat2_noreplace(source, destination)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=("Accept one signed snapshot upload. This does not connect to PostgreSQL.")
    )
    sub = parser.add_subparsers(dest="command", required=True)
    accept = sub.add_parser("accept")
    accept.add_argument("release_id")
    accept.add_argument("--incoming", type=Path, required=True)
    accept.add_argument("--accept-root", type=Path, required=True)
    accept.add_argument("--allowed-signers", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "accept":
            accept_release(
                args.release_id,
                args.incoming,
                args.accept_root,
                args.allowed_signers,
            )
            print(f"Accepted {args.release_id}.")
    except TransferAcceptError as exc:
        raise SystemExit(str(exc)) from exc


def _accept_locked(
    release_id: str,
    upload: Path,
    accept_root: Path,
    signers: Path,
) -> Path:
    names = _expected_names(release_id)
    opened = _open_upload(upload)
    staging: Path | None = None
    published = False
    try:
        listed = _classify_opened(opened, names)
        blobs: dict[str, bytes] = {}
        identities: dict[str, tuple[int, int, int]] = {}
        observed: dict[str, os.stat_result] = {}
        for name in _ordered(names):
            opened.confirm()
            blob, info = _read_verified(
                upload / name,
                _limit(release_id, name),
                dir_fd=opened.fd,
                listed=listed[name],
            )
            blobs[name] = blob
            identities[name] = (info.st_dev, info.st_ino, info.st_size)
            observed[name] = info
        _require_commit_mtimes(observed)
        opened.confirm()
        staging_root = accept_root / "staging"
        _discard_stale(staging_root, release_id)
        staging = staging_root / f"{release_id}.{os.getpid()}.{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        _chmod(staging, 0o700)
        destination = accept_root / "accepted" / release_id
        written = _write_snapshot(staging, release_id, blobs)
        _confirm_unchanged(opened, upload, blobs, identities, release_id)
        _match_commit(release_id, blobs, names)
        manifest_name = f"{release_id}.json"
        dump_name = f"{release_id}.dump"
        signature_name = f"{release_id}.json.sig"
        _verify_staged(
            staging / manifest_name,
            staging / signature_name,
            staging / dump_name,
            signers,
            written[manifest_name],
            written[signature_name],
            written[dump_name],
            release_id,
        )
        ready = _ready_bytes(release_id)
        _write_new(staging / READY_NAME, ready)
        written[READY_NAME] = ready
        _restrict(staging)
        _fsync_tree(staging)
        published = _publish_directory(staging, destination)
        if not published:
            _require_same_release(staging, destination, written)
        _chmod(destination, 0o550)
        return destination
    finally:
        opened.close()
        if staging is not None and not published and (staging.is_symlink() or staging.exists()):
            _make_tree_writable(staging)
            _discard(staging)


def _verify_staged(
    manifest_path: Path,
    signature_path: Path,
    dump_path: Path,
    signers: Path,
    manifest_bytes: bytes,
    signature_bytes: bytes,
    dump_bytes: bytes,
    release_id: str,
) -> None:
    if not _same_bytes(manifest_path.read_bytes(), manifest_bytes):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    if not _same_bytes(signature_path.read_bytes(), signature_bytes):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    if not _same_bytes(dump_path.read_bytes(), dump_bytes):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    try:
        verify_manifest(manifest_path, signature_path, signers)
        payload = load_manifest(manifest_path)
        verify_dump(dump_path, payload)
    except SnapshotError as exc:
        raise TransferAcceptError(str(exc)) from exc
    if str(payload["release_id"]) != release_id:
        raise TransferAcceptError(
            "The copied manifest release id does not match the upload directory."
        )
    if not _same_bytes(manifest_path.read_bytes(), manifest_bytes):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    if not _same_bytes(dump_path.read_bytes(), dump_bytes):
        raise TransferAcceptError("A transfer file changed while it was being copied.")


def _publish_directory(staging: Path, destination: Path) -> bool:
    try:
        publish_without_replace(staging, destination)
    except FileExistsError:
        return False
    except OSError as exc:
        raise TransferAcceptError("The accepted release could not be published.") from exc
    _fsync_directory(destination.parent)
    return True


def _renameat2_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise TransferAcceptError(
            "Accepted releases are published only with Linux renameat2(RENAME_NOREPLACE)."
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(destination))
    raise OSError(error, os.strerror(error), str(source), None, str(destination))


def _require_same_release(
    staging: Path,
    destination: Path,
    written: Mapping[str, bytes],
) -> None:
    if destination.is_symlink() or not destination.is_dir():
        raise TransferAcceptError("A directory without READY is not an accepted release.")
    present = _directory_names(destination)
    required = set(written)
    if present != required or any((destination / name).is_symlink() for name in present):
        if READY_NAME not in present:
            raise TransferAcceptError("A directory without READY is not an accepted release.")
        raise TransferAcceptError("An accepted release with different bytes already exists.")
    for name, payload in written.items():
        current = _read_limited(destination / name, len(payload))
        if current is None or not _same_bytes(current, payload):
            if name == READY_NAME and not (destination / READY_NAME).is_file():
                raise TransferAcceptError("A directory without READY is not an accepted release.")
            raise TransferAcceptError("An accepted release with different bytes already exists.")


def _read_limited(path: Path, expected_size: int) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        return None
    if info.st_size > MAX_DUMP_BYTES:
        return None
    return path.read_bytes()


def _write_snapshot(
    staging: Path,
    release_id: str,
    blobs: Mapping[str, bytes],
) -> dict[str, bytes]:
    written: dict[str, bytes] = {}
    for partial_name, payload in blobs.items():
        if partial_name == COMMIT_NAME:
            continue
        published = partial_name[: -len(".partial")]
        if published != f"{release_id}.dump" and not published.startswith(f"{release_id}.json"):
            raise TransferAcceptError("The upload contains an unexpected file.")
        _write_new(staging / published, payload)
        written[published] = payload
    return written


def _match_commit(
    release_id: str,
    blobs: Mapping[str, bytes],
    names: set[str],
) -> None:
    declared = _parse_commit(release_id, blobs[COMMIT_NAME], names)
    for name, (size, digest) in declared.items():
        payload = blobs[name]
        if size != len(payload) or not _same_hex(digest, hashlib.sha256(payload).hexdigest()):
            raise TransferAcceptError("COMMIT.partial does not match the copied bytes.")


def _parse_commit(release_id: str, blob: bytes, names: set[str]) -> dict[str, tuple[int, str]]:
    if len(blob) > MAX_COMMIT_BYTES:
        raise TransferAcceptError("A transfer file is larger than the acceptance limit.")
    try:
        payload = json.loads(blob.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, TransferAcceptError) as exc:
        raise TransferAcceptError("COMMIT.partial is not a completion marker.") from exc
    if not isinstance(payload, dict):
        raise TransferAcceptError("COMMIT.partial is not a completion marker.")
    if set(payload) != {"marker", "release_id", "files"}:
        raise TransferAcceptError("COMMIT.partial is not a completion marker.")
    if payload["marker"] != _COMMIT_MARKER or payload["release_id"] != release_id:
        raise TransferAcceptError("COMMIT.partial is not a completion marker.")
    files = payload["files"]
    if not isinstance(files, list):
        raise TransferAcceptError("COMMIT.partial is not a completion marker.")
    expected = names - {COMMIT_NAME}
    found: dict[str, tuple[int, str]] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"name", "size", "sha256_hex"}:
            raise TransferAcceptError("COMMIT.partial is not a completion marker.")
        name = item["name"]
        size = item["size"]
        digest = item["sha256_hex"]
        if (
            not isinstance(name, str)
            or name not in expected
            or name in found
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > _limit(release_id, name)
            or not isinstance(digest, str)
            or _hex_digest(digest) is None
        ):
            raise TransferAcceptError("COMMIT.partial is not a completion marker.")
        found[name] = (size, digest)
    if set(found) != expected:
        raise TransferAcceptError("COMMIT.partial is not a completion marker.")
    return found


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise TransferAcceptError("COMMIT.partial is not a completion marker.")
    return dict(pairs)


def _confirm_unchanged(
    opened: _OpenedUpload,
    upload: Path,
    blobs: Mapping[str, bytes],
    identities: Mapping[str, tuple[int, int, int]],
    release_id: str,
) -> None:
    for name, payload in blobs.items():
        opened.confirm()
        again, info = _read_verified(
            upload / name,
            _limit(release_id, name),
            dir_fd=opened.fd,
        )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TransferAcceptError("A transfer file changed while it was being copied.")
        if (info.st_dev, info.st_ino, info.st_size) != identities[name]:
            raise TransferAcceptError("A transfer file changed while it was being copied.")
        if not _same_bytes(again, payload):
            raise TransferAcceptError("A transfer file changed while it was being copied.")


def _read_verified(
    path: Path,
    limit: int,
    *,
    dir_fd: int | None = None,
    listed: os.stat_result | None = None,
) -> tuple[bytes, os.stat_result]:
    """Read a regular file twice. Inode and size are not the only check.

    dir_fd is the upload directory opened with O_DIRECTORY | O_NOFOLLOW.
    The name is opened relative to that descriptor, so a later rename of the
    directory path cannot substitute another directory.
    """
    if dir_fd is not None:
        _reject_component(path.name)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            handle = os.open(path.name, flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, getattr(errno, "EMLINK", 0), getattr(errno, "EFTYPE", 0)}:
                raise TransferAcceptError("The upload contains a symlink.") from exc
            raise TransferAcceptError("A transfer file could not be read.") from exc
        try:
            return _read_checked_handle(handle, limit, listed)
        finally:
            os.close(handle)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TransferAcceptError("A transfer file is not a regular file.")
    if info.st_size > limit:
        raise TransferAcceptError("A transfer file is larger than the acceptance limit.")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    elif os.name == "posix":
        raise TransferAcceptError("This host cannot refuse a symlink open.")
    try:
        handle = os.open(path, flags)
    except OSError as exc:
        symlink_errors = {errno.ELOOP, getattr(errno, "EMLINK", 0), getattr(errno, "EFTYPE", 0)}
        if exc.errno in symlink_errors:
            raise TransferAcceptError("The upload contains a symlink.") from exc
        raise TransferAcceptError("A transfer file could not be read.") from exc
    try:
        blob, first = _read_checked_handle(handle, limit, info)
    finally:
        os.close(handle)
    if (first.st_dev, first.st_ino, first.st_size) != (info.st_dev, info.st_ino, info.st_size):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    return blob, first


def _read_checked_handle(
    handle: int,
    limit: int,
    listed: os.stat_result | None,
) -> tuple[bytes, os.stat_result]:
    first = os.fstat(handle)
    if stat.S_ISLNK(first.st_mode) or not stat.S_ISREG(first.st_mode):
        raise TransferAcceptError("A transfer file is not a regular file.")
    if listed is not None and (first.st_dev, first.st_ino, first.st_size) != (
        listed.st_dev,
        listed.st_ino,
        listed.st_size,
    ):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    if first.st_size > limit:
        raise TransferAcceptError("A transfer file is larger than the acceptance limit.")
    blob = _read_exact(handle, first.st_size, limit)
    second = os.fstat(handle)
    if (second.st_dev, second.st_ino, second.st_size) != (
        first.st_dev,
        first.st_ino,
        first.st_size,
    ):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    os.lseek(handle, 0, os.SEEK_SET)
    again = _read_exact(handle, second.st_size, limit)
    if not _same_bytes(blob, again):
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    return blob, first


def _read_exact(handle: int, size: int, limit: int) -> bytes:
    if size < 0 or size > limit:
        raise TransferAcceptError("A transfer file is larger than the acceptance limit.")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        piece = os.read(handle, min(remaining, 1024 * 1024))
        if not piece:
            break
        remaining -= len(piece)
        chunks.append(piece)
    blob = b"".join(chunks)
    if len(blob) != size:
        raise TransferAcceptError("A transfer file changed while it was being copied.")
    return blob


class _OpenedUpload:
    """One upload directory held open for the whole copy.

    On Linux the descriptor is O_DIRECTORY | O_NOFOLLOW. File names are
    opened relative to it. Renaming the path does not change which directory
    is read. Without that open, the path inode is checked again before use.
    """

    def __init__(self, path: Path, fd: int | None, dev: int, ino: int) -> None:
        self.path = path
        self.fd = fd
        self.dev = dev
        self.ino = ino

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def confirm(self) -> None:
        try:
            info = os.fstat(self.fd) if self.fd is not None else os.lstat(self.path)
        except OSError as exc:
            raise TransferAcceptError(
                "The upload directory changed while it was being read."
            ) from exc
        if self.fd is None and stat.S_ISLNK(info.st_mode):
            raise TransferAcceptError("The upload directory changed while it was being read.")
        if (info.st_dev, info.st_ino) != (self.dev, self.ino) or not stat.S_ISDIR(info.st_mode):
            raise TransferAcceptError("The upload directory changed while it was being read.")

    def entries(self) -> list[tuple[str, os.stat_result]]:
        self.confirm()
        found: list[tuple[str, os.stat_result]] = []
        if self.fd is not None:
            try:
                names = os.listdir(self.fd)
            except OSError as exc:
                raise TransferAcceptError("The upload directory could not be read.") from exc
            for name in names:
                _reject_component(name)
                try:
                    info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                except OSError as exc:
                    raise TransferAcceptError("A transfer file could not be read.") from exc
                found.append((name, info))
        else:
            for entry in os.scandir(self.path):
                _reject_component(entry.name)
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise TransferAcceptError("A transfer file could not be read.") from exc
                found.append((entry.name, info))
        self.confirm()
        return found


def _open_upload(path: Path) -> _OpenedUpload:
    if path.is_symlink() or not path.is_dir():
        raise TransferAcceptError("The upload directory is not a real directory.")
    fd = _open_directory_fd(path)
    if fd is None:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TransferAcceptError("The upload directory is not a real directory.")
        return _OpenedUpload(path, None, info.st_dev, info.st_ino)
    try:
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise TransferAcceptError("The upload directory could not be read.") from exc
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise TransferAcceptError("The upload directory is not a real directory.")
    return _OpenedUpload(path, fd, info.st_dev, info.st_ino)


def _open_directory_fd(path: Path) -> int | None:
    """Open path with O_DIRECTORY and O_NOFOLLOW, or return None off Linux."""
    if os.name != "posix":
        return None
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not nofollow:
        raise TransferAcceptError("This host cannot refuse a replaced upload directory.")
    flags = os.O_RDONLY | directory_flag | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(os.fspath(path), flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", 0)}:
            raise TransferAcceptError("The upload directory is not a real directory.") from exc
        raise TransferAcceptError("The upload directory could not be read.") from exc


def _classify_opened(opened: _OpenedUpload, names: set[str]) -> dict[str, os.stat_result]:
    found: dict[str, os.stat_result] = {}
    for name, info in opened.entries():
        if stat.S_ISLNK(info.st_mode):
            raise TransferAcceptError("The upload contains a symlink.")
        if name not in names or not stat.S_ISREG(info.st_mode):
            raise TransferAcceptError("The upload contains an unexpected file.")
        found[name] = info
    if set(found) != names:
        raise TransferAcceptError("The upload is missing a file.")
    return found


def _require_commit_mtimes(observed: Mapping[str, os.stat_result]) -> None:
    commit_mtime = observed[COMMIT_NAME].st_mtime_ns
    for name, info in observed.items():
        if name != COMMIT_NAME and info.st_mtime_ns > commit_mtime:
            raise TransferAcceptError("COMMIT.partial must be uploaded last.")


def _reject_component(name: str) -> None:
    if name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise TransferAcceptError("The upload contains an unexpected file.")


def _expected_names(release_id: str) -> set[str]:
    return {
        f"{release_id}.dump.partial",
        f"{release_id}.json.partial",
        f"{release_id}.json.sig.partial",
        COMMIT_NAME,
    }


def _ordered(names: set[str]) -> tuple[str, ...]:
    commit = (COMMIT_NAME,) if COMMIT_NAME in names else ()
    rest = tuple(sorted(names - {COMMIT_NAME}))
    return commit + rest


def _limit(release_id: str, name: str) -> int:
    if name == f"{release_id}.dump.partial":
        return MAX_DUMP_BYTES
    if name == f"{release_id}.json.partial":
        return MAX_MANIFEST_BYTES
    if name == f"{release_id}.json.sig.partial":
        return MAX_SIGNATURE_BYTES
    if name == COMMIT_NAME:
        return MAX_COMMIT_BYTES
    raise TransferAcceptError("The upload contains an unexpected file.")


def _ready_bytes(release_id: str) -> bytes:
    return f"{_READY_PREFIX}{release_id}\n".encode("ascii")


def _canonical_uuid(value: str) -> str:
    if not isinstance(value, str) or _UUID_TEXT.fullmatch(value) is None:
        raise TransferAcceptError("The release id must be a canonical lowercase UUID.")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise TransferAcceptError("The release id must be a canonical lowercase UUID.") from exc
    if str(parsed) != value:
        raise TransferAcceptError("The release id must be a canonical lowercase UUID.")
    return value


def _upload_directory(incoming: Path, release_id: str) -> Path:
    directory = incoming / release_id
    if directory.parent != incoming:
        raise TransferAcceptError("The release id must be a canonical lowercase UUID.")
    if directory.is_symlink():
        raise TransferAcceptError("The upload directory is not a real directory.")
    if not directory.is_dir():
        raise TransferAcceptError("The upload directory is missing.")
    return directory


def _real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise TransferAcceptError(f"The {label} directory is not a real directory.")
    return path.resolve()


def _separate(incoming: Path, accept_root: Path) -> None:
    if incoming == accept_root or _within(incoming, accept_root) or _within(accept_root, incoming):
        raise TransferAcceptError(
            "The transfer directory and the acceptance directory must be separate."
        )


def _require_pin(path: Path, incoming: Path, accept_root: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise TransferAcceptError("The allowed signers file is missing.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TransferAcceptError("The allowed signers file must be a regular file.")
    if info.st_size == 0:
        raise TransferAcceptError("The allowed signers file is missing.")
    _assert_pin_not_replaceable(path)
    absolute = Path(os.path.abspath(path))
    if (
        _within(absolute, incoming)
        or _within(absolute, Path(os.path.abspath(accept_root / "staging")))
        or _within(absolute, Path(os.path.abspath(accept_root / "accepted")))
        or _within(absolute, Path(os.path.abspath(accept_root / "locks")))
    ):
        raise TransferAcceptError("The allowed signers file must be pinned outside the upload.")


def _assert_pin_not_replaceable(path: Path) -> None:
    """Refuse a pin the acceptance user can rewrite or replace.

    The walk uses lstat from the file through the filesystem root. A parent
    the user can write is enough to replace a read-only file. The sticky bit
    is honored: it stops that user from unlinking a child they do not own.
    A symlink anywhere in the chain is refused. A path containing '..' is
    refused so lexical cleanup cannot skip one of those components. Windows
    has no owner bits, so this walk runs on POSIX. Publication on Windows is
    refused separately.
    """
    if ".." in Path(os.fspath(path)).parts:
        raise TransferAcceptError("The allowed signers path must not contain '..'.")
    creds = _posix_credentials()
    if creds is None:
        return
    uid, gids = creds
    current = Path(os.path.abspath(path))
    try:
        info = os.lstat(current)
    except OSError as exc:
        raise TransferAcceptError("The allowed signers path could not be checked.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TransferAcceptError("The allowed signers file must be a regular file.")
    if _has_write(info, uid, gids):
        raise TransferAcceptError(
            "The allowed signers file must not be writable by the acceptance user."
        )
    child = info
    while True:
        parent = current.parent
        if parent == current:
            break
        try:
            parent_info = os.lstat(parent)
        except OSError as exc:
            raise TransferAcceptError("The allowed signers path could not be checked.") from exc
        if stat.S_ISLNK(parent_info.st_mode):
            raise TransferAcceptError("The allowed signers path contains a symlink.")
        if not stat.S_ISDIR(parent_info.st_mode):
            raise TransferAcceptError("The allowed signers path is not inside a real directory.")
        if _can_replace_entry(parent_info, child, uid, gids):
            raise TransferAcceptError("The acceptance user can replace the allowed signers file.")
        child = parent_info
        current = parent


def _posix_credentials() -> tuple[int, set[int]] | None:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or os.name != "posix":
        return None
    uid = geteuid()
    gids: set[int] = set()
    getegid = getattr(os, "getegid", None)
    if getegid is not None:
        gids.add(getegid())
    getgroups = getattr(os, "getgroups", None)
    if getgroups is not None:
        try:
            gids.update(getgroups())
        except OSError:
            pass
    return uid, gids


def _has_write(info: os.stat_result, uid: int, gids: set[int]) -> bool:
    mode = info.st_mode
    if info.st_uid == uid and mode & stat.S_IWUSR:
        return True
    if info.st_gid in gids and mode & stat.S_IWGRP:
        return True
    return bool(mode & stat.S_IWOTH)


def _can_replace_entry(
    directory: os.stat_result,
    child: os.stat_result,
    uid: int,
    gids: set[int],
) -> bool:
    if not _has_write(directory, uid, gids):
        return False
    if directory.st_uid == uid:
        return True
    if directory.st_mode & stat.S_ISVTX and child.st_uid != uid:
        return False
    return True


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _prepare_accept_root(root: Path) -> None:
    _mkdir_mode(root / "staging", 0o700)
    _mkdir_mode(root / "accepted", 0o750)


def _mkdir_mode(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, mode)


def _chmod(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def _write_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(path, flags, 0o400)
    except OSError as exc:
        raise TransferAcceptError("A staged file could not be written.") from exc
    try:
        view = payload
        while view:
            written = os.write(handle, view)
            if written <= 0:
                raise TransferAcceptError("A staged file could not be written.")
            view = view[written:]
        os.fsync(handle)
    finally:
        os.close(handle)
    _chmod(path, 0o440)


def _restrict(directory: Path) -> None:
    """Make staged files read-only and leave the directory writable.

    Linux rename of a directory needs write permission on that directory so it
    can update the .. entry. The published directory is sealed after the rename.
    """
    for child in directory.iterdir():
        if child.is_symlink():
            raise TransferAcceptError("A staged file could not be written.")
        _chmod(child, 0o440)


def _fsync_tree(directory: Path) -> None:
    """Flush a directory on POSIX. File bytes were already flushed at write.

    Windows refuses os.fsync on a read-only handle. The writable handle in
    the staging write already flushed those bytes.
    """
    if os.name != "posix":
        return
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            raise TransferAcceptError("A staged file could not be written.")
        handle = os.open(child, os.O_RDONLY)
        try:
            os.fsync(handle)
        except OSError as exc:
            raise TransferAcceptError("A staged file could not be written.") from exc
        finally:
            os.close(handle)
    _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _discard_stale(staging_root: Path, release_id: str) -> None:
    if not staging_root.is_dir():
        return
    prefix = f"{release_id}."
    for child in staging_root.iterdir():
        if child.name.startswith(prefix):
            _discard(child)


def _make_tree_writable(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        _chmod(path, 0o700)
        return
    _chmod(path, 0o700)
    for child in path.iterdir():
        if not child.is_symlink():
            _chmod(child, 0o700)


def _remove_readonly(function: Callable[[str], object], path: str, _exc: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    function(path)


def _discard(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, onexc=_remove_readonly)
        return
    _chmod(path, 0o700)
    path.unlink()


def _directory_names(directory: Path) -> set[str]:
    names: set[str] = set()
    for entry in os.scandir(directory):
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise TransferAcceptError("An accepted release with different bytes already exists.")
        names.add(entry.name)
    return names


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        raise TransferAcceptError("Another acceptance is running for this release.")
    handle = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(handle, 0o600)
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)
    finally:
        os.close(handle)


def _lock_handle(handle: int) -> None:
    if os.name == "posix":
        import fcntl

        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransferAcceptError("Another acceptance is running for this release.") from exc
        return
    import msvcrt

    if os.fstat(handle).st_size < 1:
        os.write(handle, b"\0")
    os.lseek(handle, 0, os.SEEK_SET)
    try:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise TransferAcceptError("Another acceptance is running for this release.") from exc


def _unlock_handle(handle: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    import msvcrt

    os.lseek(handle, 0, os.SEEK_SET)
    try:
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    except OSError:
        return


def _same_bytes(left: bytes, right: bytes) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _same_hex(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


def _hex_digest(value: str) -> str | None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        return None
    return value


if __name__ == "__main__":
    main(sys.argv[1:])
