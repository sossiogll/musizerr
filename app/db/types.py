"""
Column types shared by the models.

SQLite has no datetime type: SQLAlchemy stores the text of the value without
its zone and reads it back NAIVE. Every datetime Musizeer stores is UTC, so
``UTCDateTime`` gives it back aware (UTC) and stores any aware value
converted to UTC: whatever serializes it then says which zone it is in.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A UTC datetime: stored as the naive UTC text (the format already on
    disk, so no migration), read back aware."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
