"""Monotonic ingestion checkpoints.

A checkpoint moves only when the caller commits a durable date. An HTTP
response, by itself, does not move it.
"""

from __future__ import annotations

from datetime import date

from openfactors.ingestion.errors import ConnectorError, ErrorCategory

NATIONAL_LOCATION = "_national"


class CheckpointKey:
    def __init__(self, source_id: str, location_id: str, stream: str) -> None:
        if not source_id or not location_id or not stream:
            raise ConnectorError(ErrorCategory.CONFIG, "Checkpoint identity is incomplete.")
        self.source_id = source_id
        self.location_id = location_id
        self.stream = stream

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CheckpointKey)
            and self.source_id == other.source_id
            and self.location_id == other.location_id
            and self.stream == other.stream
        )

    def __hash__(self) -> int:
        return hash((self.source_id, self.location_id, self.stream))


def job_identity(
    source_id: str,
    location_id: str,
    stream: str,
    interval_start: date,
    interval_end: date,
) -> str:
    if interval_start > interval_end:
        raise ConnectorError(ErrorCategory.CONFIG, "Interval start is after interval end.")
    return "|".join(
        (
            source_id,
            location_id,
            stream,
            interval_start.isoformat(),
            interval_end.isoformat(),
        )
    )


class MemoryCheckpoints:
    def __init__(self) -> None:
        self._dates: dict[CheckpointKey, date] = {}

    def get(self, key: CheckpointKey) -> date | None:
        return self._dates.get(key)

    def commit(self, key: CheckpointKey, durable_date: date) -> date:
        current = self._dates.get(key)
        if current is not None and durable_date < current:
            raise ConnectorError(
                ErrorCategory.CHECKPOINT_REGRESSION,
                "Checkpoint date moved backward.",
            )
        self._dates[key] = durable_date
        return durable_date
