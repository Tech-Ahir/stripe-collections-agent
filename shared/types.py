"""A datetime column that cannot lose its timezone.

SQLite has no native datetime type, and SQLAlchemy's default mapping round-trips
``DateTime`` through a naive string. That would make ``expires_at < now_utc()`` raise, or
worse, silently compare a UTC instant against a local-time reading. Storing an explicit
ISO-8601 UTC string fixes it, sorts correctly under a plain lexicographic ORDER BY, and
behaves identically on Postgres.

``format_utc`` is also what the audit chain hashes, so a row's stored timestamp and its
hashed timestamp are the same bytes by construction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import String, TypeDecorator

from shared.clock import as_utc

FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_utc(value: datetime) -> str:
    """The one canonical string form of an instant in this system."""
    return as_utc(value).strftime(FORMAT)


def parse_utc(value: str) -> datetime:
    return as_utc(datetime.strptime(value, FORMAT))


class UtcDateTime(TypeDecorator):
    """Timezone-aware UTC datetime stored as a sortable ISO-8601 string."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("expected datetime, got " + type(value).__name__)
        return format_utc(value)

    def process_result_value(self, value: str | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return parse_utc(value)
