"""Синтетические каналы с заранее заложенными аутлайерами (прямо в БД, без API)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from radar.db import Database
from radar.schemas import Channel, ChannelStatus, Video, VideoFormat, VideoSnapshot


def add_channel(db: Database, cid: str, now: datetime, subs: int = 100_000) -> Channel:
    ch = Channel(
        id=cid,
        title=cid,
        subs=subs,
        uploads_playlist_id="UU" + cid,
        niche_ids=["ai-tools"],
        status=ChannelStatus.WATCHING,
        added_at=now - timedelta(days=90),
    )
    return db.upsert_channel(ch, now, keep_status=False)


def add_video(
    db: Database,
    cid: str,
    vid: str,
    published: datetime,
    snaps: list[tuple[datetime, int]],
    fmt: VideoFormat = VideoFormat.LONG,
) -> None:
    db.upsert_video(
        Video(
            id=vid,
            channel_id=cid,
            title=f"Видео {vid}",
            duration_sec=600 if fmt == VideoFormat.LONG else 40,
            format=fmt,
            published_at=published,
            thumbnail_url=f"https://i.ytimg.com/vi/{vid}/hq.jpg",
        ),
        published,
    )
    db.add_snapshots(VideoSnapshot(video_id=vid, views=v, collected_at=t) for t, v in snaps)


def growth(day: float) -> float:
    """Типичная кривая: к 7 дню ~100% «зрелых» просмотров."""
    return min(1.0, 1 - 0.85 * pow(2.718, -day / 1.8)) if day > 0 else 0.0


def mature_channel(
    db: Database,
    cid: str,
    now: datetime,
    *,
    base: int,
    n: int,
    fmt=VideoFormat.LONG,
    sigma: float = 0.25,
    seed: int = 1,
    spacing_days: float = 3,
    start_days: float = 8,
    subs: int = 100_000,
    history: bool = False,
) -> list[str]:
    """n зрелых видео канала. history=True — снимки каждые 6 ч первые 14 дней (для velocity)."""
    rnd = random.Random(seed)
    add_channel(db, cid, now, subs=subs)
    ids = []
    for i in range(n):
        pub = now - timedelta(days=start_days + i * spacing_days)
        final = int(base * rnd.lognormvariate(0, sigma))
        vid = f"{cid}-{fmt.value[0]}{i:02d}"
        if history:
            snaps = [
                (pub + timedelta(hours=h), int(final * growth(h / 24)))
                for h in range(6, 24 * 14, 6)
                if pub + timedelta(hours=h) <= now
            ]
        else:
            snaps = []
        snaps.append((now, final))
        add_video(db, cid, vid, pub, snaps, fmt)
        ids.append(vid)
    return ids


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
