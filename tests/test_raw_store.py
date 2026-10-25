"""Failure injection for the raw artifact commit."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openfactors.ingestion.checkpoints import CheckpointKey, MemoryCheckpoints
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.raw_store import (
    FileObjectStore,
    RawArtifact,
    RetentionLabel,
    commit_raw_artifact,
)

RUN_ID = "00000000-0000-4000-8000-000000000014"
RETRIEVED = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)
PAYLOAD = b'{"daily":{"time":["2018-01-01"]}}'
FINGERPRINT = "open-meteo-era5|ph-manila|daily|2018-01-01|2018-01-01"
KEEP = RetentionLabel("until_source_withdrawn", None)


class MemoryObjects:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.puts = 0

    def put_if_absent(self, key: str, body: bytes) -> None:
        self.puts += 1
        current = self.bodies.get(key)
        if current is None:
            self.bodies[key] = body
            return
        if current != body:
            raise ConnectorError(
                ErrorCategory.CHECKSUM, "Object key already holds different bytes."
            )

    def get(self, key: str) -> bytes:
        try:
            return self.bodies[key]
        except KeyError as exc:
            raise ConnectorError(ErrorCategory.PARTIAL_RESPONSE, "Raw object is missing.") from exc


class MemoryManifests:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], RawArtifact] = {}
        self.fail_inserts = 0

    def find(self, run_id: str, request_fingerprint: str) -> RawArtifact | None:
        return self.rows.get((run_id, request_fingerprint))

    def insert(self, artifact: RawArtifact) -> RawArtifact:
        if self.fail_inserts:
            self.fail_inserts -= 1
            raise ConnectorError(ErrorCategory.NETWORK, "injected manifest failure")
        key = (artifact.run_id, artifact.request_fingerprint)
        if key in self.rows:
            raise ConnectorError(ErrorCategory.SCHEMA, "Manifest already recorded.")
        self.rows[key] = artifact
        return artifact


class CorruptRead:
    def __init__(self, inner: MemoryObjects) -> None:
        self.inner = inner

    def put_if_absent(self, key: str, body: bytes) -> None:
        self.inner.put_if_absent(key, body)

    def get(self, key: str) -> bytes:
        return self.inner.get(key) + b"x"


def _commit(objects, manifests, body: bytes = PAYLOAD, **overrides) -> RawArtifact:
    arguments = {
        "run_id": RUN_ID,
        "source_id": "open-meteo-era5",
        "body": body,
        "content_type": "application/json",
        "request_fingerprint": FINGERPRINT,
        "retrieved_at": RETRIEVED,
        "source_updated_at": None,
        "response_status": 200,
        "retention": KEEP,
    }
    arguments.update(overrides)
    return commit_raw_artifact(objects, manifests, **arguments)


def test_commit_verifies_bytes_and_leaves_retention_open() -> None:
    objects = MemoryObjects()
    manifests = MemoryManifests()
    checkpoints = MemoryCheckpoints()
    artifact = _commit(objects, manifests)
    assert artifact.sha256_hex == hashlib.sha256(PAYLOAD).hexdigest()
    assert artifact.bytes_stored == len(PAYLOAD)
    assert artifact.retention_until is None
    assert artifact.source_updated_at is None
    assert objects.get(artifact.object_key) == PAYLOAD
    assert checkpoints.get(CheckpointKey("open-meteo-era5", "ph-manila", "daily")) is None


def test_checksum_mismatch_does_not_record_a_manifest() -> None:
    objects = MemoryObjects()
    manifests = MemoryManifests()
    with pytest.raises(ConnectorError) as caught:
        _commit(CorruptRead(objects), manifests)
    assert caught.value.category is ErrorCategory.CHECKSUM
    assert manifests.rows == {}


def test_retry_after_manifest_failure_reuses_the_object() -> None:
    objects = MemoryObjects()
    manifests = MemoryManifests()
    manifests.fail_inserts = 1
    with pytest.raises(ConnectorError) as caught:
        _commit(objects, manifests)
    assert caught.value.category is ErrorCategory.NETWORK
    recovered = _commit(objects, manifests)
    again = _commit(objects, manifests)
    assert recovered.artifact_id == again.artifact_id
    assert objects.puts == 2
    assert list(objects.bodies.values()) == [PAYLOAD]


def test_retry_with_different_bytes_is_rejected() -> None:
    objects = MemoryObjects()
    manifests = MemoryManifests()
    _commit(objects, manifests)
    with pytest.raises(ConnectorError) as caught:
        _commit(objects, manifests, body=b"other")
    assert caught.value.category is ErrorCategory.CHECKSUM
    assert len(manifests.rows) == 1


def test_scheduled_deletion_is_rejected() -> None:
    objects = MemoryObjects()
    manifests = MemoryManifests()
    with pytest.raises(ConnectorError) as caught:
        _commit(objects, manifests, retention=RetentionLabel("days", 30))
    assert caught.value.category is ErrorCategory.CONFIG
    assert objects.bodies == {}
    assert manifests.rows == {}


def test_fingerprint_and_retrieval_time_are_rejected() -> None:
    objects = MemoryObjects()
    manifests = MemoryManifests()
    with pytest.raises(ConnectorError) as caught:
        _commit(objects, manifests, request_fingerprint="https://example.test/path?token=1")
    assert caught.value.category is ErrorCategory.CONFIG
    with pytest.raises(ConnectorError) as caught:
        _commit(objects, manifests, source_updated_at=RETRIEVED)
    assert caught.value.category is ErrorCategory.CONFIG
    assert manifests.rows == {}


def test_file_store_keeps_the_payload_when_the_partial_write_stops(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    store = FileObjectStore(root)
    manifests = MemoryManifests()

    class StopBeforeLink(FileObjectStore):
        def put_if_absent(self, key: str, body: bytes) -> None:
            path = self._resolve(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".partial")
            partial.write_bytes(body)
            raise ConnectorError(ErrorCategory.NETWORK, "injected partial write")

    with pytest.raises(ConnectorError):
        _commit(StopBeforeLink(root), manifests)
    assert list(root.rglob("*"))
    assert not any(
        path.is_file() and not path.name.endswith(".partial") for path in root.rglob("*")
    )
    assert manifests.rows == {}

    artifact = _commit(store, manifests)
    assert store.get(artifact.object_key) == PAYLOAD
    hidden_partials = [
        path
        for path in root.rglob("*")
        if path.name.startswith(".") and path.name.endswith(".partial")
    ]
    assert hidden_partials == []
