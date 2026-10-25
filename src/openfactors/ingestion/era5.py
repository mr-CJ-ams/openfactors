"""Open-Meteo ERA5 daily weather for the pilot locations.

The request asks for model era5 and timezone Asia/Manila. The returned
latitude and longitude are the snapped ERA5 cell, not the city reference point.
A missing daily value stays null. A reported zero stays zero.
This source does not send an upstream update time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openfactors.ingestion.errors import ConnectorError, ErrorCategory, UpstreamTimeout
from openfactors.ingestion.http_client import HttpConnector, HttpRequest, HttpResponse

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SOURCE_ID = "open-meteo-era5"
TIMEZONE = "Asia/Manila"
UTC_OFFSET_SECONDS = 28800
MODEL = "era5"
DAILY_VARIABLES = ("temperature_2m_mean", "precipitation_sum")
GRID_NOTE = "ERA5 0.25 degree cell, about 25 km. Reanalysis grid, not a station."


@dataclass(frozen=True)
class DailyWeather:
    location_id: str
    date: date
    temperature_mean_c: Decimal | None
    precipitation_mm: Decimal | None
    source_updated_at: None = None


@dataclass(frozen=True)
class Era5Slice:
    location_id: str
    grid_latitude: Decimal
    grid_longitude: Decimal
    elevation_m: Decimal
    rows: tuple[DailyWeather, ...]

    @property
    def provider_location_ref(self) -> str:
        return f"{_grid(self.grid_latitude)},{_grid(self.grid_longitude)}"

    @property
    def weather_grid_description(self) -> str:
        elevation = _plain(self.elevation_m)
        return f"{GRID_NOTE} Snapped point {self.provider_location_ref}. Elevation {elevation} m."


def build_archive_request(
    latitude: Decimal,
    longitude: Decimal,
    start: date,
    end: date,
) -> HttpRequest:
    if start > end:
        raise ConnectorError(ErrorCategory.CONFIG, "Weather interval start is after end.")
    query = urlencode(
        {
            "latitude": _plain(latitude),
            "longitude": _plain(longitude),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": ",".join(DAILY_VARIABLES),
            "timezone": TIMEZONE,
            "models": MODEL,
        }
    )
    return HttpRequest(
        "GET",
        f"{ARCHIVE_URL}?{query}",
        headers=(("User-Agent", "OpenFactors/0.1 (non-commercial local ingest)"),),
    )


def parse_era5_daily(location_id: str, body: bytes) -> Era5Slice:
    try:
        payload = json.loads(body, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 response is not JSON.") from exc
    if not isinstance(payload, dict):
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 response is not an object.")
    _require_timezone(payload)
    _require_units(payload)
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 response has no daily object.")
    times = _series(daily, "time")
    temperatures = _series(daily, "temperature_2m_mean")
    precipitation = _series(daily, "precipitation_sum")
    if not (len(times) == len(temperatures) == len(precipitation)):
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 daily arrays have different lengths.")
    rows: list[DailyWeather] = []
    seen: list[date] = []
    for raw_day, raw_temp, raw_precip in zip(times, temperatures, precipitation, strict=True):
        if not isinstance(raw_day, str):
            raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 daily date is not text.")
        try:
            local_day = date.fromisoformat(raw_day)
        except ValueError as exc:
            raise ConnectorError(
                ErrorCategory.SCHEMA, "ERA5 daily date is not a local date."
            ) from exc
        seen.append(local_day)
        rows.append(
            DailyWeather(
                location_id=location_id,
                date=local_day,
                temperature_mean_c=_measure(raw_temp, "temperature"),
                precipitation_mm=_precipitation(raw_precip),
            )
        )
    if seen != sorted(set(seen)):
        raise ConnectorError(
            ErrorCategory.SCHEMA, "ERA5 daily dates are not a unique ordered series."
        )
    return Era5Slice(
        location_id=location_id,
        grid_latitude=_coordinate(payload.get("latitude"), "latitude"),
        grid_longitude=_coordinate(payload.get("longitude"), "longitude"),
        elevation_m=_coordinate(payload.get("elevation"), "elevation"),
        rows=tuple(rows),
    )


def fetch_era5_daily(
    connector: HttpConnector,
    *,
    location_id: str,
    latitude: Decimal,
    longitude: Decimal,
    start: date,
    end: date,
) -> Era5Slice:
    request = build_archive_request(latitude, longitude, start, end)
    result = connector.fetch(request)
    return parse_era5_daily(location_id, result.body)


def urllib_transport(request: HttpRequest, timeout: float) -> HttpResponse:
    outbound = Request(request.url, method=request.method, headers=dict(request.headers))
    try:
        with urlopen(outbound, timeout=timeout) as response:
            return HttpResponse(
                response.status,
                response.read(),
                tuple(response.headers.items()),
            )
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.read(), tuple(exc.headers.items()))
    except TimeoutError as exc:
        raise UpstreamTimeout from exc
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise UpstreamTimeout from exc
        raise OSError(str(exc.reason)) from exc


def _require_timezone(payload: dict) -> None:
    if payload.get("timezone") != TIMEZONE:
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 timezone is not Asia/Manila.")
    if payload.get("utc_offset_seconds") != UTC_OFFSET_SECONDS:
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 offset is not +08:00.")


def _require_units(payload: dict) -> None:
    units = payload.get("daily_units")
    if not isinstance(units, dict):
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 response has no daily units.")
    if units.get("temperature_2m_mean") != "°C":
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 temperature unit is not °C.")
    if units.get("precipitation_sum") != "mm":
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 precipitation unit is not mm.")


def _series(daily: dict, name: str) -> list:
    values = daily.get(name)
    if not isinstance(values, list):
        raise ConnectorError(ErrorCategory.SCHEMA, f"ERA5 daily field {name} is missing.")
    return values


def _measure(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConnectorError(ErrorCategory.SCHEMA, f"ERA5 {label} is not a number.")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    raise ConnectorError(ErrorCategory.SCHEMA, f"ERA5 {label} is not a number.")


def _precipitation(value: object) -> Decimal | None:
    number = _measure(value, "precipitation")
    if number is not None and number < 0:
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 precipitation is negative.")
    return number


def _coordinate(value: object, label: str) -> Decimal:
    number = _measure(value, label)
    if number is None:
        raise ConnectorError(ErrorCategory.SCHEMA, f"ERA5 {label} is missing.")
    return number


def _grid(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _plain(value: Decimal) -> str:
    try:
        text = format(value, "f")
    except InvalidOperation as exc:
        raise ConnectorError(ErrorCategory.SCHEMA, "ERA5 coordinate is not a decimal.") from exc
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
