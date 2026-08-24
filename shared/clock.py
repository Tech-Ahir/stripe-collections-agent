"""Time, in one place.

Every timestamp in this system is UTC and timezone-aware. Naive datetimes are not
allowed past this module, because the proposal TTL and the approval-token expiry are
security-relevant comparisons and a silent local-time reading would break both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def now_utc() -> datetime:
    """The current instant, timezone-aware, UTC."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; convert an aware one. Never guesses local time."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def hours_from_now(hours: float) -> datetime:
    return now_utc() + timedelta(hours=hours)


def seconds_from_now(seconds: float) -> datetime:
    return now_utc() + timedelta(seconds=seconds)


def to_epoch(value: datetime) -> int:
    return int(as_utc(value).timestamp())


def from_epoch(value: int | float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)
