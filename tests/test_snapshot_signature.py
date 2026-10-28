"""Offline Ed25519 signatures for an existing snapshot manifest."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from ops.publish_snapshot import (
    SIGNATURE_NAMESPACE,
    SIGNATURE_PRINCIPAL,
    SnapshotError,
    _publish_new_signature,
    sign_manifest,
    signature_path_for,
    verify_manifest,
)

_MARKER = 'namespaces="openfactors-snapshot"'


def test_a_signed_manifest_verifies(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, '{"principal":"other","allowed_signers":"C:/evil"}\n')
    key = _key(tmp_path, "publisher")
    signers = _signers(tmp_path, key)
    signature = sign_manifest(manifest, key)
    assert signature == signature_path_for(manifest)
    assert signature.name == "manifest.json.sig"
    verify_manifest(manifest, signature, signers)


def test_a_changed_manifest_byte_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, '{"release_id":"abc"}\n')
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    manifest.write_bytes(manifest.read_bytes().replace(b"abc", b"abd"))
    with pytest.raises(SnapshotError, match="rejected"):
        verify_manifest(manifest, signature, _signers(tmp_path, key))


def test_a_wrong_signing_key_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    publisher = _key(tmp_path, "publisher")
    other = _key(tmp_path, "other")
    signature = sign_manifest(manifest, publisher)
    with pytest.raises(SnapshotError, match="rejected"):
        verify_manifest(manifest, signature, _signers(tmp_path, other))


def test_a_wrong_principal_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    signers = _signers(tmp_path, key, principal="other-publisher")
    with pytest.raises(SnapshotError, match="invalid"):
        verify_manifest(manifest, signature, signers)


def test_a_wrong_namespace_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    _sign_with_namespace(manifest, key, "other-namespace")
    with pytest.raises(SnapshotError, match="rejected"):
        verify_manifest(manifest, signature_path_for(manifest), _signers(tmp_path, key))


def test_a_missing_signature_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    with pytest.raises(SnapshotError, match="missing"):
        verify_manifest(manifest, signature_path_for(manifest), _signers(tmp_path, key))


def test_a_malformed_signature_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = signature_path_for(manifest)
    signature.write_text("not a signature\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="malformed"):
        verify_manifest(manifest, signature, _signers(tmp_path, key))


def test_an_empty_allowed_signers_file_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    signers = tmp_path / "allowed_signers"
    signers.write_text("", encoding="utf-8")
    with pytest.raises(SnapshotError, match="missing"):
        verify_manifest(manifest, signature, signers)


def test_a_directory_is_not_an_allowed_signers_file(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    with pytest.raises(SnapshotError, match="missing"):
        verify_manifest(manifest, signature, tmp_path)


def test_an_unreadable_allowed_signers_file_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    signers = _signers(tmp_path, key)
    original = Path.read_text

    def fail_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == signers:
            raise OSError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(SnapshotError, match="cannot be read"):
        verify_manifest(manifest, signature, signers)


def test_a_keygen_timeout_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)

    def expire(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="ssh-keygen", timeout=30)

    monkeypatch.setattr("ops.publish_snapshot.subprocess.run", expire)
    with pytest.raises(SnapshotError, match="did not finish"):
        verify_manifest(manifest, signature, _signers(tmp_path, key))


def test_a_missing_ssh_keygen_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    monkeypatch.setattr("ops.publish_snapshot.shutil.which", lambda _name: None)
    with pytest.raises(SnapshotError, match="not available"):
        verify_manifest(manifest, signature, _signers(tmp_path, key))


def test_an_unexpected_success_line_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)

    def accept(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b'Good "other" signature for someone with RSA key\n',
            stderr=b"",
        )

    monkeypatch.setattr("ops.publish_snapshot.subprocess.run", accept)
    with pytest.raises(SnapshotError, match="rejected"):
        verify_manifest(manifest, signature, _signers(tmp_path, key))


def test_verify_pins_the_principal_namespace_and_signers_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(
        tmp_path,
        '{"principal":"attacker","namespace":"other","allowed_signers":"C:/evil"}\n',
    )
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    signers = _signers(tmp_path, key)
    captured: dict[str, object] = {}
    original = subprocess.run

    def spy(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["args"] = list(args)
        captured["input"] = kwargs.get("input")
        captured["shell"] = kwargs.get("shell", False)
        return original(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("ops.publish_snapshot.subprocess.run", spy)
    verify_manifest(manifest, signature, signers)
    args = captured["args"]
    assert isinstance(args, list)
    assert captured["shell"] is False
    assert captured["input"] == manifest.read_bytes()
    assert args[args.index("-I") + 1] == SIGNATURE_PRINCIPAL
    assert args[args.index("-n") + 1] == SIGNATURE_NAMESPACE
    assert args[args.index("-f") + 1] == str(signers)
    assert "C:/evil" not in args


def test_an_existing_signature_is_left_in_place(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    original = signature.read_bytes()
    with pytest.raises(SnapshotError, match="already exists"):
        sign_manifest(manifest, key)
    assert signature.read_bytes() == original


def test_a_signing_key_inside_the_repository_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    key = repo / "publisher"
    key.write_text("not-a-private-key", encoding="utf-8")
    manifest = _manifest(tmp_path, "{}\n")
    monkeypatch.setattr("ops.publish_snapshot.ROOT", repo)
    with pytest.raises(SnapshotError, match="outside Git"):
        sign_manifest(manifest, key)


def test_an_unusable_signing_key_hides_key_material(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    decoy = tmp_path / "not-a-key"
    decoy.write_text("not-a-private-key\n", encoding="utf-8")
    os.chmod(decoy, 0o600)
    with pytest.raises(SnapshotError, match="could not be used") as caught:
        sign_manifest(manifest, decoy)
    message = str(caught.value)
    assert "PRIVATE KEY" not in message
    assert "not-a-private-key" not in message
    assert not signature_path_for(manifest).exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_a_failed_sign_can_be_retried(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    decoy = tmp_path / "not-a-key"
    decoy.write_text("not-a-private-key\n", encoding="utf-8")
    os.chmod(decoy, 0o600)
    with pytest.raises(SnapshotError, match="could not be used"):
        sign_manifest(manifest, decoy)
    assert not signature_path_for(manifest).exists()
    key = _key(tmp_path, "publisher")
    signature = sign_manifest(manifest, key)
    verify_manifest(manifest, signature, _signers(tmp_path, key))
    kept = signature.read_bytes()
    with pytest.raises(SnapshotError, match="already exists"):
        sign_manifest(manifest, key)
    assert signature.read_bytes() == kept
    assert list(tmp_path.glob("*.partial")) == []


def test_a_failed_publish_removes_the_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "manifest.json.sig"

    def fail(_source: object, _target: object) -> None:
        raise OSError("disk")

    publication = "link" if os.name == "posix" else "rename"
    monkeypatch.setattr(f"ops.publish_snapshot.os.{publication}", fail)
    payload = b"-----BEGIN SSH SIGNATURE-----\n-----END SSH SIGNATURE-----\n"
    with pytest.raises(SnapshotError, match="could not be written"):
        _publish_new_signature(destination, payload)
    assert not destination.exists()
    assert list(tmp_path.glob("*.partial")) == []


def test_publish_does_not_replace_an_existing_signature(tmp_path: Path) -> None:
    destination = tmp_path / "manifest.json.sig"
    payload = b"-----BEGIN SSH SIGNATURE-----\nkeep\n-----END SSH SIGNATURE-----\n"
    _publish_new_signature(destination, payload)
    with pytest.raises(SnapshotError, match="already exists"):
        _publish_new_signature(destination, payload + b"changed\n")
    assert destination.read_bytes() == payload
    assert list(tmp_path.glob("*.partial")) == []


def test_a_group_readable_signing_key_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = tmp_path / "publisher"
    key.write_text("x\n", encoding="utf-8")
    original = Path.stat

    def stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        info = original(self, *args, **kwargs)
        if self == key:
            return os.stat_result((0o100644,) + tuple(info)[1:])
        return info

    monkeypatch.setattr("ops.publish_snapshot._private_key_mode_applies", lambda: True)
    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(SnapshotError, match="other accounts") as caught:
        sign_manifest(manifest, key)
    assert "x" not in str(caught.value)
    assert not signature_path_for(manifest).exists()


def test_a_symlink_signing_key_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "{}\n")
    key = tmp_path / "publisher"
    key.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self, **_kwargs: self == key)
    with pytest.raises(SnapshotError, match="regular file"):
        sign_manifest(manifest, key)
    assert not signature_path_for(manifest).exists()


def _manifest(directory: Path, text: str) -> Path:
    path = directory / "manifest.json"
    path.write_bytes(text.encode("utf-8"))
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
            "openfactors-publisher",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0 or not path.is_file():
        raise AssertionError("The test signing key could not be created.")
    return path


def _signers(directory: Path, key_path: Path, *, principal: str = "openfactors-publisher") -> Path:
    public = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
    path = directory / f"{key_path.name}-allowed_signers"
    path.write_text(f"{principal} {_MARKER} {public}\n", encoding="utf-8")
    return path


def _sign_with_namespace(manifest: Path, key_path: Path, namespace: str) -> None:
    completed = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(key_path),
            "-n",
            namespace,
            str(manifest),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError("The test signature could not be created.")
