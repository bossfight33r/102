"""Discovery: search.list по seed_queries ниш → новые каналы-кандидаты в диапазоне подписчиков."""

from __future__ import annotations

from datetime import datetime, timedelta

from radar.config import AppConfig
from radar.db import Database
from radar.log import get_logger
from radar.schemas import ChannelStatus, FormatPref, Niche, TaskResult
from radar.timeutil import quota_day
from radar.youtube.client import QuotaExceededError, YouTube, YouTubeAPIError
from radar.youtube.quota import DISCOVERY_PREFIX, QuotaDeferred

log = get_logger(__name__)


def searches_done_today(db: Database, yt: YouTube, niche: Niche, now: datetime) -> int:
    units = db.quota_used(
        quota_day(now), purpose_prefix=f"{DISCOVERY_PREFIX}:{niche.id}", method="search.list"
    )
    return units // yt.planner.cost("search.list")


def queries_for_today(niche: Niche, done: int, now: datetime) -> list[str]:
    """Ротация запросов по дням: за несколько дней проходим все seed_queries."""
    q = niche.seed_queries
    remaining = niche.discovery_per_day - done
    if not q or remaining <= 0:
        return []
    start = quota_day(now).toordinal() * niche.discovery_per_day + done
    return [q[(start + k) % len(q)] for k in range(min(remaining, len(q)))]


def run_discovery(
    yt: YouTube, db: Database, cfg: AppConfig, niches: list[Niche], now: datetime
) -> TaskResult:
    status = ChannelStatus.WATCHING if cfg.discovery.auto_approve else ChannelStatus.CANDIDATE
    searches = added = rejected = 0
    for niche in niches:
        if not niche.enabled:
            continue
        purpose = f"{DISCOVERY_PREFIX}:{niche.id}"
        queries = queries_for_today(niche, searches_done_today(db, yt, niche, now), now)
        if not queries:
            continue
        known = db.channel_ids()
        new_channel_ids: set[str] = set()
        deferred_msg = ""
        for q in queries:
            try:
                hits = yt.search(
                    q,
                    purpose=purpose,
                    now=now,
                    published_after=now - timedelta(days=cfg.discovery.published_within_days),
                    region_code=niche.region or None,
                    relevance_language=niche.language or None,
                    video_duration="short" if niche.format == FormatPref.SHORTS else None,
                    max_results=cfg.discovery.max_results,
                )
            except (QuotaDeferred, QuotaExceededError) as e:
                deferred_msg = str(e)
                break
            except YouTubeAPIError as e:
                log.warning("discovery_search_failed", niche=niche.id, reason=e.reason)
                continue
            searches += 1
            new_channel_ids |= {h.channel_id for h in hits if h.channel_id not in known}
        # Найденное дорогими search сохраняем, даже если следующий search отложен.
        try:
            channels = yt.get_channels(
                sorted(new_channel_ids),
                purpose=purpose,
                now=now,
                niche_ids=[niche.id],
                status=status,
            )
        except (QuotaDeferred, QuotaExceededError) as e:
            deferred_msg = deferred_msg or str(e)
            channels = []
        with db.tx():
            for ch in channels:
                if ch.subs is None or not (niche.min_subs <= ch.subs <= niche.max_subs):
                    rejected += 1
                    continue
                db.upsert_channel(ch, now)
                added += 1
        log.info(
            "discovery_niche_done", niche=niche.id, queries=len(queries), candidates=len(channels)
        )
        if deferred_msg:
            return TaskResult(
                name="discovery",
                stats={"searches": searches, "added": added, "rejected": rejected},
                deferred=True,
                message=deferred_msg,
            )
    return TaskResult(
        name="discovery", stats={"searches": searches, "added": added, "rejected": rejected}
    )


def discovery_pending(yt: YouTube, db: Database, niches: list[Niche], now: datetime) -> bool:
    return any(
        n.enabled and queries_for_today(n, searches_done_today(db, yt, n, now), now) for n in niches
    )
