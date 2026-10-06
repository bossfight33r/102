"""Снимки статистики: videos.list батчами по 50, частота зависит от возраста видео."""

from __future__ import annotations

from datetime import datetime, timedelta

from radar.config import AppConfig
from radar.db import Database
from radar.schemas import TaskResult, VideoSnapshot
from radar.youtube.client import BATCH_SIZE, QuotaExceededError, YouTube, chunked
from radar.youtube.quota import QuotaDeferred

# tick идёт каждые 15 минут: допуск, чтобы интервал 6 ч не превращался в 6 ч 15 мин.
TOLERANCE = timedelta(minutes=10)


def due_video_ids(db: Database, cfg: AppConfig, now: datetime) -> list[str]:
    s = cfg.snapshots
    out: list[str] = []
    for vid, published_at, last_at in db.videos_for_snapshots(now - timedelta(days=s.mid_days)):
        age = now - published_at
        interval = timedelta(
            hours=s.fresh_interval_hours
            if age < timedelta(days=s.fresh_days)
            else s.mid_interval_hours
        )
        if last_at is None or now - last_at >= interval - TOLERANCE:
            out.append(vid)
    return out


def collect_snapshots(yt: YouTube, db: Database, cfg: AppConfig, now: datetime) -> TaskResult:
    """Новые снимки append-only. Каждый батч сохраняется сразу — откладывание не теряет прогресс."""
    ids = due_video_ids(db, cfg, now)
    saved = 0
    for batch in chunked(ids, BATCH_SIZE):
        try:
            items = yt.get_videos(batch, purpose="snapshots", now=now)
        except (QuotaDeferred, QuotaExceededError) as e:
            return TaskResult(
                name="snapshots",
                stats={"due": len(ids), "saved": saved},
                deferred=True,
                message=str(e),
            )
        with db.tx():
            for video, _ in items:
                db.upsert_video(video, now)
            saved += db.add_snapshots(
                VideoSnapshot(
                    video_id=st.video_id,
                    views=st.views,
                    likes=st.likes,
                    comments=st.comments,
                    collected_at=now,
                )
                for _, st in items
            )
    return TaskResult(name="snapshots", stats={"due": len(ids), "saved": saved})
