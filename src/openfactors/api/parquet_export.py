"""Parquet export for the same 12 public fields as the factor query.

Dates are date32. Measurements keep their decimal scale. source_updated_at is
UTC. The file metadata stores the pinned release id and the schema version.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
_PLACES = Decimal("0.001")
_SCHEMA = pa.schema(
    [
        pa.field("date", pa.date32(), nullable=False),
        pa.field("location_id", pa.string(), nullable=False),
        pa.field("country_code", pa.string(), nullable=False),
        pa.field("temperature_mean_c", pa.decimal128(7, 3), nullable=True),
        pa.field("precipitation_mm", pa.decimal128(10, 3), nullable=True),
        pa.field("is_public_holiday", pa.bool_(), nullable=False),
        pa.field("holiday_name", pa.string(), nullable=True),
        pa.field("is_weekend", pa.bool_(), nullable=False),
        pa.field("weather_source_id", pa.string(), nullable=False),
        pa.field("holiday_source_id", pa.string(), nullable=False),
        pa.field("source_updated_at", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("dataset_version", pa.string(), nullable=False),
    ]
)


def parquet_bytes(
    rows: list[dict],
    columns: tuple[str, ...],
    *,
    release_id: str,
    dataset_version: str,
) -> bytes:
    if columns != tuple(field.name for field in _SCHEMA):
        raise ValueError("Parquet columns do not match the 12 public fields.")
    schema = _SCHEMA.with_metadata(
        {
            b"dataset_version": dataset_version.encode("utf-8"),
            b"release_id": release_id.encode("utf-8"),
        }
    )
    arrays = [
        pa.array([_coerce(field.name, row.get(field.name)) for row in rows], type=field.type)
        for field in schema
    ]
    table = pa.Table.from_arrays(arrays, schema=schema)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


def read_parquet(payload: bytes) -> tuple[list[dict], dict[str, str]]:
    table = pq.read_table(io.BytesIO(payload))
    metadata = {
        key.decode("utf-8"): value.decode("utf-8")
        for key, value in (table.schema.metadata or {}).items()
        if key in {b"dataset_version", b"release_id"}
    }
    return table.to_pylist(), metadata


def _coerce(name: str, value: object) -> object:
    if name == "date":
        return date.fromisoformat(str(value))
    if name in {"temperature_mean_c", "precipitation_mm"}:
        if value is None:
            return None
        return Decimal(str(value)).quantize(_PLACES)
    if name == "source_updated_at":
        return _timestamp(value)
    if name == "holiday_name":
        return None if value is None else str(value)
    if name in {"is_public_holiday", "is_weekend"}:
        if not isinstance(value, bool):
            raise ValueError("A published boolean must already be true or false.")
        return value
    if value is None:
        raise ValueError("A required export field is null.")
    return str(value)


def _timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("source_updated_at must include a timezone.")
    return parsed.astimezone(UTC)
