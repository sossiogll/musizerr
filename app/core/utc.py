"""
UTC datetimes. Every datetime Musizeer stores is UTC; a naive one (from
SQLite, or normalized by a provider) is read as UTC here, and a datetime
leaves the API only as ISO 8601 WITH its offset: a browser reads a string
without one as local time.
"""
from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime | None) -> datetime | None:
    """``value`` aware and in UTC; a naive value is taken as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_iso(value: datetime | None) -> str | None:
    """The API form of a datetime: ISO 8601 in UTC, offset included."""
    aware = as_utc(value)
    return aware.isoformat() if aware else None
