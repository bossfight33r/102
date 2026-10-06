"""Разовый разбор любого ролика по ссылке или id (не обязательно из watchlist)."""

from __future__ import annotations

import re
from datetime import datetime

from radar.db import Database
from radar.schemas import Video, VideoSnapshot
from radar.youtube.client import YouTube

_ID = r"([A-Za-z0-9_-]{11})"
_PATTERNS = [
    re.compile(rf"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/){_ID}"),
    re.compile(rf"^{_ID}$"),
]


def parse_video_ref(ref: str) -> str | None:
    """id ролика из ссылки (watch, youtu.be, shorts, live, embed) или голого id."""
    ref = ref.strip()
    for p in _PATTERNS:
        if m := p.search(ref):
            return m.group(1)
    return None


def ensure_video(yt: YouTube, db: Database, ref: str, now: datetime) -> Video:
    """Видео из БД или одним вызовом videos.list (1 ед.) — метаданные + снимок статистики."""
    video_id = parse_video_ref(ref)
    if video_id is None:
        raise ValueError(f"не похоже на ссылку или id ролика: {ref}")
    if video := db.get_video(video_id):
        return video
    items = yt.get_videos([video_id], purpose="adhoc", now=now)
    if not items:
        raise ValueError(f"ролик {video_id} не найден (удалён или приватный)")
    video, stats = items[0]
    with db.tx():
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
    return video
