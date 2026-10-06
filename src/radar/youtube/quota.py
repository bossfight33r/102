"""Учёт квоты YouTube Data API: журнал (quota_ledger) и планировщик бюджета."""

from __future__ import annotations

from datetime import datetime

from radar.config import QuotaConfig
from radar.db import Database
from radar.log import get_logger
from radar.schemas import QuotaEntry
from radar.timeutil import ensure_utc, iso, next_quota_reset, parse_dt, quota_day

log = get_logger(__name__)

PAUSE_KEY = "quota_paused_until"
DISCOVERY_PREFIX = "discovery"


class QuotaDeferred(Exception):
    """Планировщик не разрешил вызов: задача откладывается до следующего tick/сброса квоты."""


class QuotaPlanner:
    """Решает, можно ли сейчас потратить units, и пишет каждый вызов в журнал.

    Правила:
    - дорогие методы (стоимость >= expensive_threshold) — только пока used + units <= budget - safety_margin;
    - дешёвые не трогают ещё не потраченный резерв discovery: used + units <= budget - reserve_left;
    - после quotaExceeded все вызовы запрещены до полуночи по тихоокеанскому времени.
    """

    def __init__(self, db: Database, cfg: QuotaConfig) -> None:
        self.db = db
        self.cfg = cfg

    def cost(self, method: str) -> int:
        if method not in self.cfg.costs:
            raise KeyError(f"Неизвестный метод API без стоимости в конфиге: {method}")
        return self.cfg.costs[method]

    def is_expensive(self, method: str) -> bool:
        return self.cost(method) >= self.cfg.expensive_threshold

    def used(self, now: datetime) -> int:
        return self.db.quota_used(quota_day(now))

    def discovery_used(self, now: datetime) -> int:
        return self.db.quota_used(quota_day(now), purpose_prefix=DISCOVERY_PREFIX)

    def reserve_left(self, now: datetime) -> int:
        return max(0, self.cfg.discovery_reserve - self.discovery_used(now))

    def paused_until(self, now: datetime) -> datetime | None:
        raw = self.db.get_kv(PAUSE_KEY)
        if not raw:
            return None
        until = parse_dt(raw)
        return until if until > ensure_utc(now) else None

    def limit_for(self, method: str, purpose: str, now: datetime) -> int:
        budget = self.cfg.daily_budget
        if self.is_expensive(method):
            return budget - self.cfg.safety_margin
        if purpose.startswith(DISCOVERY_PREFIX):
            return budget - self.cfg.safety_margin
        return budget - self.reserve_left(now)

    def check(self, method: str, purpose: str, now: datetime) -> int:
        units = self.cost(method)
        until = self.paused_until(now)
        if until:
            raise QuotaDeferred(f"квота исчерпана, пауза до {iso(until)}")
        used = self.used(now)
        limit = self.limit_for(method, purpose, now)
        if used + units > limit:
            raise QuotaDeferred(
                f"{method} ({units} ед.) не помещается: использовано {used}, лимит {limit}"
            )
        return units

    def calls_available(self, method: str, purpose: str, now: datetime) -> int:
        if self.paused_until(now):
            return 0
        units = self.cost(method)
        return max(0, (self.limit_for(method, purpose, now) - self.used(now)) // units)

    def record(self, method: str, purpose: str, now: datetime) -> None:
        entry = QuotaEntry(
            date=quota_day(now), method=method, units=self.cost(method), purpose=purpose
        )
        self.db.add_quota(entry, now)

    def pause_until_reset(self, now: datetime) -> datetime:
        until = next_quota_reset(now)
        self.db.set_kv(PAUSE_KEY, iso(until))
        self.db.add_alert(
            f"⚠️ YouTube API: quotaExceeded. Сбор приостановлен до {iso(until)} (сброс квоты).",
            now,
            key=f"quota_exceeded:{quota_day(now).isoformat()}",
        )
        log.warning("quota_exceeded", paused_until=iso(until))
        return until

    def status(self, now: datetime) -> dict[str, object]:
        day = quota_day(now)
        until = self.paused_until(now)
        return {
            "date": day.isoformat(),
            "budget": self.cfg.daily_budget,
            "used": self.used(now),
            "discovery_used": self.discovery_used(now),
            "discovery_reserve": self.cfg.discovery_reserve,
            "remaining": max(0, self.cfg.daily_budget - self.used(now)),
            "paused_until": iso(until) if until else None,
            "breakdown": self.db.quota_breakdown(day),
        }
