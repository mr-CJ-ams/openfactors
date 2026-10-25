"""Durable raw payload and manifest commit.

The object is written once, then read back and checked against SHA-256.
The manifest is inserted only after that check. A later retry of the same
run and request returns the recorded artifact when the bytes still match.
retention_until stays null while the approved mode is until_source_withdrawn.
This commit does not move a checkpoint.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openfactors.ingestion.errors import ConnectorError, ErrorCategory

APPROVED_RETENTION_MODE = "until_source_withdrawn"
_FINGERPRINT_BLOCKED = ("?", "#", "password", "secret", "authorization", "api_key", "api-key")


@dataclass(frozen=True)
class RetentionLabel:
    mode: str
    raw_retention_days: int | None


@dataclass(frozen=True)
class RawArtifact:
    artifact_id: str
    run_id: str
    source_id: str
    object_key: str
    sha256_hex: str
    content_type: str
    request_fingerprint: str
    retrieved_at: datetime
    source_updated_at: datetime | None
    response_status: int | None
    bytes_stored: int
    retention_until: datetime | None


class ObjectStore:
    def put_if_absent(self, key: str, body: bytes) -> None:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError


class ManifestLog:
    def find(self, run_id: str, request_fingerprint: str) -> RawArtifact | None:
        raise NotImplementedError

    def insert(self, artifact: RawArtifact) -> RawArtifact:
        raise NotImplementedError


class FileObjectStore(ObjectStore):
    def __init__(self, root: Path) -> None:
        self.root = root

    def put_if_absent(self, key: str, body: bytes) -> None:
        path = self._resolve(key)
        if path.is_file():
            self._reject_different_bytes(path, body)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        temporary.write_bytes(body)
        try:
            os.link(temporary, path)
        except FileExistsError:
            self._reject_different_bytes(path, body)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise ConnectorError(ErrorCategory.PARTIAL_RESPONSE, "Raw object is missing.")
        return path.read_bytes()

    def _resolve(self, key: str) -> Path:
        _validate_object_key(key)
        root = self.root.resolve()
        path = (root / key).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ConnectorError(ErrorCategory.CONFIG, "Object key escapes the raw root.") from exc
        return path

    def _reject_different_bytes(self, path: Path, body: bytes) -> None:
        if path.read_bytes() != body:
            raise ConnectorError(
                ErrorCategory.CHECKSUM, "Object key already holds different bytes."
            )


def commit_raw_artifact(
    objects: ObjectStore,
    manifests: ManifestLog,
    *,
    run_id: str,
    source_id: str,
    body: bytes,
    content_type: str,
    request_fingerprint: str,
    retrieved_at: datetime,
    source_updated_at: datetime | None,
    response_status: int | None,
    retention: RetentionLabel,
    artifact_id_factory: Callable[[], str] | None = None,
) -> RawArtifact:
    _require_retention(retention)
    _require_fingerprint(request_fingerprint)
    _require_times(retrieved_at, source_updated_at)
    if not isinstance(content_type, str) or not content_type.strip():
        raise ConnectorError(ErrorCategory.CONFIG, "Raw content type is empty.")
    digest = hashlib.sha256(body).hexdigest()
    key = object_key(source_id, run_id, digest)
    existing = manifests.find(run_id, request_fingerprint)
    if existing is not None:
        _require_existing(existing, digest, key, content_type)
        _verify_stored(objects, key, digest, body)
        return existing
    objects.put_if_absent(key, body)
    _verify_stored(objects, key, digest, body)
    artifact = RawArtifact(
        artifact_id=(artifact_id_factory or _new_id)(),
        run_id=run_id,
        source_id=source_id,
        object_key=key,
        sha256_hex=digest,
        content_type=content_type,
        request_fingerprint=request_fingerprint,
        retrieved_at=retrieved_at,
        source_updated_at=source_updated_at,
        response_status=response_status,
        bytes_stored=len(body),
        retention_until=None,
    )
    return manifests.insert(artifact)


def restore_raw_object(objects: ObjectStore, key: str, body: bytes, sha256_hex: str) -> bytes:
    """Put a missing raw object back when the backup bytes match the recorded digest.

    A different object already stored at that key is left unchanged.
    """
    if hashlib.sha256(body).hexdigest() != sha256_hex:
        raise ConnectorError(
            ErrorCategory.CHECKSUM, "Backup bytes do not match the recorded digest."
        )
    objects.put_if_absent(key, body)
    stored = objects.get(key)
    if hashlib.sha256(stored).hexdigest() != sha256_hex or stored != body:
        raise ConnectorError(
            ErrorCategory.CHECKSUM, "Restored bytes do not match the recorded digest."
        )
    return stored


def object_key(source_id: str, run_id: str, sha256_hex: str) -> str:
    if not _safe_token(source_id):
        raise ConnectorError(ErrorCategory.CONFIG, "Source id cannot be an object key.")
    _require_uuid(run_id, "run_id")
    if len(sha256_hex) != 64 or any(
        character not in "0123456789abcdef" for character in sha256_hex
    ):
        raise ConnectorError(ErrorCategory.SCHEMA, "SHA-256 must be 64 lowercase hex characters.")
    return f"raw/{source_id}/{run_id}/{sha256_hex}"


def _verify_stored(objects: ObjectStore, key: str, digest: str, body: bytes) -> None:
    stored = objects.get(key)
    if hashlib.sha256(stored).hexdigest() != digest or stored != body:
        raise ConnectorError(ErrorCategory.CHECKSUM, "Stored bytes do not match the payload.")


def _require_existing(existing: RawArtifact, digest: str, key: str, content_type: str) -> None:
    if existing.sha256_hex != digest or existing.object_key != key:
        raise ConnectorError(
            ErrorCategory.CHECKSUM, "Retry payload does not match the recorded artifact."
        )
    if existing.content_type != content_type:
        raise ConnectorError(
            ErrorCategory.SCHEMA, "Retry content type does not match the recorded artifact."
        )
    if existing.retention_until is not None:
        raise ConnectorError(
            ErrorCategory.CONFIG, "Recorded raw artifact has a scheduled deletion."
        )


def _require_retention(retention: RetentionLabel) -> None:
    if retention.mode != APPROVED_RETENTION_MODE or retention.raw_retention_days is not None:
        raise ConnectorError(
            ErrorCategory.CONFIG,
            "Raw files are kept until the source is withdrawn.",
        )


def _require_fingerprint(fingerprint: str) -> None:
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ConnectorError(ErrorCategory.CONFIG, "Request fingerprint is empty.")
    lowered = fingerprint.casefold()
    if any(token in lowered for token in _FINGERPRINT_BLOCKED):
        raise ConnectorError(
            ErrorCategory.CONFIG, "Request fingerprint must not carry a query or secret."
        )


def _require_times(retrieved_at: datetime, source_updated_at: datetime | None) -> None:
    if retrieved_at.tzinfo is None:
        raise ConnectorError(ErrorCategory.CONFIG, "retrieved_at must include a timezone.")
    if source_updated_at is not None and source_updated_at.tzinfo is None:
        raise ConnectorError(ErrorCategory.CONFIG, "source_updated_at must include a timezone.")
    if source_updated_at == retrieved_at:
        raise ConnectorError(ErrorCategory.CONFIG, "source_updated_at must not copy retrieved_at.")


def _require_uuid(value: str, label: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(ErrorCategory.CONFIG, f"{label} is not a UUID.") from exc
    if str(parsed) != value:
        raise ConnectorError(ErrorCategory.CONFIG, f"{label} is not a lowercase UUID.")


def _safe_token(value: str) -> bool:
    if not value or value[0] == "-" or value[-1] == "-":
        return False
    return all(
        character.islower() or character.isdigit() or character == "-" for character in value
    )


def _validate_object_key(key: str) -> None:
    parts = key.split("/")
    if (
        not key
        or "\\" in key
        or key.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ConnectorError(ErrorCategory.CONFIG, "Object key is not a relative raw path.")


def _new_id() -> str:
    return str(uuid.uuid4())
