"""Watchlist: новые видео отслеживаемых каналов через uploads-плейлисты (не через search)."""

from __future__ import annotations

from datetime import datetime, timedelta

from radar.config import AppConfig
from radar.db import Database
from radar.log import get_logger
from radar.schemas import Channel, ChannelStatus, Niche, TaskResult, VideoSnapshot
from radar.youtube.client import QuotaExceededError, YouTube, YouTubeAPIError
from radar.youtube.quota import QuotaDeferred

log = get_logger(__name__)

SEED_RETRY_DAYS = 7


def _seed_pending(db: Database, niche: Niche, ref: str, now: datetime) -> bool:
    mark = db.get_kv(f"seed:{niche.id}:{ref.lower()}")
    return not mark or (mark.startswith("notfound:") and _expired(mark, now))


def seeds_pending(db: Database, niches: list[Niche], now: datetime) -> bool:
    return any(
        _seed_pending(db, n, ref, now) for n in niches if n.enabled for ref in n.seed_channels
    )


def add_seed_channels(yt: YouTube, db: Database, niches: list[Niche], now: datetime) -> TaskResult:
    """seed_channels ниш → watching. Уже разрешённые ссылки не запрашиваются повторно."""
    added = 0
    for niche in niches:
        if not niche.enabled:
            continue
        for ref in niche.seed_channels:
            key = f"seed:{niche.id}:{ref.lower()}"
            if not _seed_pending(db, niche, ref, now):
                continue
            try:
                ch = yt.resolve_channel(
                    ref, purpose=f"seed:{niche.id}", now=now, niche_ids=[niche.id]
                )
            except (QuotaDeferred, QuotaExceededError) as e:
                return TaskResult(
                    name="seed_channels", stats={"added": added}, deferred=True, message=str(e)
                )
            if ch is None:
                db.set_kv(key, f"notfound:{now.date().isoformat()}")
                log.warning("seed_channel_not_found", niche=niche.id, ref=ref)
                continue
            existing = db.get_channel(ch.id)
            if existing and existing.status == ChannelStatus.HIDDEN:
                db.set_kv(key, ch.id)  # скрыт вручную — не возвращаем
                continue
            db.upsert_channel(ch, now, keep_status=False)
            db.set_kv(key, ch.id)
            added += 1
    return TaskResult(name="seed_channels", stats={"added": added})


def _expired(mark: str, now: datetime) -> bool:
    day = datetime.fromisoformat(mark.split(":", 1)[1]).date()
    return (now.date() - day).days >= SEED_RETRY_DAYS


def due_channels(
    db: Database, cfg: AppConfig, now: datetime, *, force: bool = False
) -> list[Channel]:
    interval = timedelta(minutes=cfg.watchlist.interval_minutes)
    times = db.channel_poll_times()
    return [
        c
        for c in db.list_channels(status=ChannelStatus.WATCHING)
        if force or times[c.id][0] is None or now - times[c.id][0] >= interval  # type: ignore[operator]
    ]


def stale_subs(db: Database, cfg: AppConfig, now: datetime) -> list[str]:
    max_age = timedelta(hours=cfg.watchlist.channel_refresh_hours)
    times = db.channel_poll_times()
    return [
        c.id
        for c in db.list_channels(status=ChannelStatus.WATCHING)
        if times[c.id][1] is None or now - times[c.id][1] >= max_age  # type: ignore[operator]
    ]


def watchlist_due(db: Database, cfg: AppConfig, now: datetime) -> bool:
    return bool(due_channels(db, cfg, now) or stale_subs(db, cfg, now))


def poll_watchlist(
    yt: YouTube, db: Database, cfg: AppConfig, now: datetime, *, force: bool = False
) -> TaskResult:
    """Опрос uploads-плейлистов каналов, у которых подошёл срок; новые видео + первый снимок.

    Первая страница плейлиста (до 50 видео, 1 ед.) даёт и свежие ролики, и историю для базлайна.
    Канал помечается опрошенным только после сохранения его видео — при откладывании повторим.
    """
    due = due_channels(db, cfg, now, force=force)
    listed: list[str] = []
    new_ids: list[str] = []
    deferred_msg = ""
    for ch in due:
        try:
            uploads = yt.list_uploads(
                ch.uploads_playlist_id, purpose="watchlist", now=now, max_items=50
            )
        except (QuotaDeferred, QuotaExceededError) as e:
            deferred_msg = str(e)
            break
        except YouTubeAPIError as e:
            # Плейлист пуст/удалён: помечаем опрошенным, иначе каждый tick тратит квоту впустую.
            log.warning("uploads_failed", channel=ch.id, reason=e.reason)
            db.set_channel_polled(ch.id, now)
            continue
        known = db.existing_video_ids(v for v, _ in uploads)
        new_ids += [v for v, _ in uploads if v not in known]
        listed.append(ch.id)

    saved = 0
    if new_ids:
        try:
            items = yt.get_videos(new_ids, purpose="watchlist", now=now)
        except (QuotaDeferred, QuotaExceededError) as e:
            return TaskResult(
                name="watchlist",
                stats={"channels": 0, "new_videos": 0},
                deferred=True,
                message=str(e),
            )
        with db.tx():
            for video, stats in items:
                db.upsert_video(video, now)
                db.add_snapshots(
                    [
                        VideoSnapshot(
                            video_id=video.id,
                            views=stats.views,
                            likes=stats.likes,
                            comments=stats.comments,
                            collected_at=now,
                        )
                    ]
                )
                saved += 1
    with db.tx():
        for cid in listed:
            db.set_channel_polled(cid, now)

    subs = refresh_channel_stats(yt, db, cfg, now)
    return TaskResult(
        name="watchlist",
        stats={"channels": len(listed), "new_videos": saved, "subs_refreshed": subs},
        deferred=bool(deferred_msg),
        message=deferred_msg,
    )


def refresh_channel_stats(yt: YouTube, db: Database, cfg: AppConfig, now: datetime) -> int:
    """Подписчики watching-каналов раз в channel_refresh_hours (channels.list батчами по 50)."""
    stale = stale_subs(db, cfg, now)
    if not stale:
        return 0
    try:
        fresh = yt.get_channels(stale, purpose="channel_stats", now=now)
    except (QuotaDeferred, QuotaExceededError):
        return 0
    with db.tx():
        for ch in fresh:
            db.update_channel_subs(ch.id, ch.subs, now)
    return len(fresh)
