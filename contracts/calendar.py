"""Asia/Manila calendar dates. The offset is fixed at UTC+8, with no daylight-saving change."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

MANILA = timezone(timedelta(hours=8))
ISO_SATURDAY = 6
ISO_SUNDAY = 7


def manila_date(instant: str) -> date:
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A Manila date is derived from a timezone-aware instant.")
    return parsed.astimezone(MANILA).date()


def manila_today(now: datetime | None = None) -> date:
    current = datetime.now(MANILA) if now is None else now
    if current.tzinfo is None:
        raise ValueError("A Manila date is derived from a timezone-aware instant.")
    return current.astimezone(MANILA).date()


def date_parts(local_date: date) -> dict[str, date | int | bool]:
    day_of_week = local_date.isoweekday()
    return {
        "date": local_date,
        "day_of_week": day_of_week,
        "is_weekend": day_of_week in (ISO_SATURDAY, ISO_SUNDAY),
        "calendar_year": local_date.year,
        "calendar_month": local_date.month,
    }
