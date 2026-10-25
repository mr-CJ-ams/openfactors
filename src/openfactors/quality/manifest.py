"""Candidate release manifest. The content id is not the schema version.

A rejected manifest is kept for audit. This module does not publish rows or
move the active release pointer.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from openfactors.quality.coverage import CoverageResult, evaluate_coverage
from openfactors.quality.gates import GateResult, evaluate

DATASET_ID = "daily_external_factors_ph"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WEATHER_STATUS = frozenset({"available", "missing", "upstream_gap"})
_PLACES = Decimal("0.001")
_CONTENT_FIELDS = (
    "date",
    "location_id",
    "country_code",
    "temperature_mean_c",
    "precipitation_mm",
    "is_public_holiday",
    "holiday_name",
    "is_weekend",
    "weather_source_id",
    "holiday_source_id",
    "source_updated_at",
    "dataset_version",
    "weather_data_status",
)
_DECIMAL_FIELDS = frozenset({"temperature_mean_c", "precipitation_mm"})


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    dataset_id: str
    schema_version: str
    status: str
    source_manifest_set_hash: str
    content_sha256_hex: str
    expected_row_count: int
    actual_row_count: int
    coverage_start: str
    coverage_end: str
    validated_at: str
    schema_gate: GateResult
    coverage: CoverageResult
    version_mismatch: bool
    weather_status_invalid: bool

    def evidence(self) -> dict:
        return {
            "schema_gate": {
                "status": self.schema_gate.status,
                "violations": list(self.schema_gate.violations),
            },
            "coverage_gate": {
                "status": self.coverage.status,
                "violations": list(self.coverage.violations),
                "expected": self.coverage.expected,
                "present": self.coverage.present,
                "excluded": self.coverage.excluded,
                "weather_present": self.coverage.weather_present,
            },
            "version_mismatch": self.version_mismatch,
            "weather_status_invalid": self.weather_status_invalid,
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "content_sha256_hex": self.content_sha256_hex,
            "source_manifest_set_hash": self.source_manifest_set_hash,
        }


def build_manifest(
    rows: list[dict],
    columns: list[dict],
    *,
    contract_fields: list[dict],
    locations: set[str],
    dates: set[str],
    sources: set[str],
    supported: set[tuple[str, str]],
    exclusions: list[dict],
    verified_dates: dict[str, dict],
    artifacts: list[dict],
    schema_version: str,
    coverage_start: str,
    coverage_end: str,
    release_id: str | None = None,
    validated_at: str | None = None,
) -> ReleaseManifest:
    if _SEMVER.fullmatch(schema_version) is None:
        raise ValueError("schema_version is a semantic version, separate from the release id.")
    start = date.fromisoformat(coverage_start)
    end = date.fromisoformat(coverage_end)
    if start > end:
        raise ValueError("Coverage starts after it ends.")
    chosen = str(uuid.uuid4()) if release_id is None else str(uuid.UUID(release_id))
    if chosen == schema_version:
        raise ValueError("schema_version is not a release id.")
    schema_gate = evaluate(
        rows,
        columns,
        contract_fields=contract_fields,
        locations=locations,
        dates=dates,
        sources=sources,
    )
    coverage = evaluate_coverage(
        rows,
        supported=supported,
        exclusions=exclusions,
        verified_dates=verified_dates,
    )
    version_mismatch = any(row.get("dataset_version") != schema_version for row in rows)
    weather_status_invalid = any(
        row.get("weather_data_status") not in _WEATHER_STATUS for row in rows
    )
    approved = (
        schema_gate.status == "PASS"
        and coverage.status == "PASS"
        and not version_mismatch
        and not weather_status_invalid
    )
    stamp = validated_at or datetime.now(UTC).isoformat()
    return ReleaseManifest(
        chosen,
        DATASET_ID,
        schema_version,
        "approved" if approved else "rejected",
        source_manifest_hash(artifacts),
        content_sha256(rows),
        coverage.expected,
        len(rows),
        start.isoformat(),
        end.isoformat(),
        stamp,
        schema_gate,
        coverage,
        version_mismatch,
        weather_status_invalid,
    )


def content_sha256(rows: list[dict]) -> str:
    return _sha256(canonical_rows(rows))


def source_manifest_hash(artifacts: list[dict]) -> str:
    return _sha256(canonical_artifacts(artifacts))


def canonical_rows(rows: list[dict]) -> list[dict]:
    normalized = [_normalize(row) for row in rows]
    return sorted(
        normalized,
        key=lambda item: (
            item["location_id"] or "",
            item["date"] or "",
            _dumps(item),
        ),
    )


def canonical_artifacts(artifacts: list[dict]) -> list[dict]:
    collapsed: dict[str, dict] = {}
    for item in artifacts:
        artifact_id = str(uuid.UUID(str(item["artifact_id"])))
        checksum = str(item["sha256_hex"]).strip().lower()
        source_id = str(item["source_id"])
        if _SHA256.fullmatch(checksum) is None:
            raise ValueError("A source manifest checksum must be 64 lowercase hex characters.")
        record = {
            "artifact_id": artifact_id,
            "sha256_hex": checksum,
            "source_id": source_id,
        }
        previous = collapsed.get(artifact_id)
        if previous is not None and previous != record:
            raise ValueError("One artifact id has two checksums.")
        collapsed[artifact_id] = record
    return [collapsed[key] for key in sorted(collapsed)]


def _normalize(row: dict) -> dict:
    normalized: dict[str, object] = {}
    for name in _CONTENT_FIELDS:
        value = row.get(name)
        if name in _DECIMAL_FIELDS:
            normalized[name] = _decimal_text(value)
        elif name in {"country_code", "location_id", "date", "dataset_version"}:
            normalized[name] = None if value is None else str(value).strip()
        elif name == "holiday_name":
            normalized[name] = _name(value)
        elif name == "source_updated_at":
            normalized[name] = _timestamp(value)
        elif name == "weather_data_status":
            normalized[name] = None if value is None else str(value)
        else:
            normalized[name] = value
    return normalized


def _decimal_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        return format(number.quantize(_PLACES), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


def _sha256(payload: object) -> str:
    return hashlib.sha256(_dumps(payload).encode("ascii")).hexdigest()


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
