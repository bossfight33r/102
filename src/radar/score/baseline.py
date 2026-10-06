"""Базлайн канала: медиана и MAD лог-просмотров зрелых видео того же формата."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime

from radar.config import ScoringConfig
from radar.schemas import ChannelBaseline, VideoFormat

MAD_SCALE = 1.4826  # MAD → σ для нормального распределения


class Sample:
    """Зрелое видео канала для базлайна."""

    __slots__ = ("age_days", "video_id", "views")

    def __init__(self, video_id: str, views: int, age_days: float) -> None:
        self.video_id = video_id
        self.views = views
        self.age_days = age_days


def median(xs: Sequence[float]) -> float:
    return float(statistics.median(xs))


def mad(xs: Sequence[float]) -> float:
    """Median absolute deviation (без масштабного множителя)."""
    m = median(xs)
    return median([abs(x - m) for x in xs])


def log_views(v: float) -> float:
    return math.log(max(v, 1.0))


def pick_samples(
    mature: Sequence[Sample], cfg: ScoringConfig, exclude: str | None = None
) -> list[Sample]:
    """Последние N зрелых видео без оцениваемого. mature отсортирован от новых к старым."""
    return [s for s in mature if s.video_id != exclude][: cfg.baseline_videos]


def compute_baseline(
    channel_id: str,
    fmt: VideoFormat,
    samples: Sequence[Sample],
    cfg: ScoringConfig,
    now: datetime,
) -> ChannelBaseline | None:
    if not samples:
        return None
    views = [float(s.views) for s in samples]
    logs = [log_views(v) for v in views]
    per_day = [s.views / max(s.age_days, 1.0) for s in samples]
    return ChannelBaseline(
        channel_id=channel_id,
        format=fmt,
        median_views=max(median(views), 1.0),
        mad=mad(logs),
        sample_size=len(samples),
        reliable=len(samples) >= cfg.min_sample,
        computed_at=now,
        median_views_per_day=median(per_day),
    )


def ratio(views: float, baseline: ChannelBaseline) -> float:
    return views / max(baseline.median_views, 1.0)


def robust_z(views: float, baseline: ChannelBaseline, cfg: ScoringConfig) -> float:
    """z = (ln v − ln median) / (1.4826 · max(MAD_ln, min_log_mad))."""
    spread = MAD_SCALE * max(baseline.mad, cfg.min_log_mad)
    return (log_views(views) - log_views(baseline.median_views)) / spread
