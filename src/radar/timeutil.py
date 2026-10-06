from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_dt(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def quota_day(now: datetime, tz: ZoneInfo = PACIFIC) -> date:
    """Квота YouTube сбрасывается в полночь по тихоокеанскому времени."""
    return ensure_utc(now).astimezone(tz).date()


def next_quota_reset(now: datetime, tz: ZoneInfo = PACIFIC) -> datetime:
    local = ensure_utc(now).astimezone(tz)
    nxt = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return nxt.astimezone(UTC)


def age_days(published_at: datetime, now: datetime) -> float:
    return max((ensure_utc(now) - ensure_utc(published_at)).total_seconds() / 86400.0, 0.0)
