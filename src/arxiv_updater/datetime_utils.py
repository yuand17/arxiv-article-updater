"""Datetime helpers for SQLite values that lose their timezone offset."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def as_utc(value: datetime | None) -> datetime | None:
    """Return an aware UTC datetime, treating legacy SQLite values as UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_local_datetime(
    value: datetime | None,
    timezone_name: str,
    pattern: str = "%Y-%m-%d %H:%M",
) -> str:
    """Format a stored UTC timestamp in the configured display timezone."""

    aware = as_utc(value)
    if aware is None:
        return ""
    return aware.astimezone(ZoneInfo(timezone_name)).strftime(pattern)
