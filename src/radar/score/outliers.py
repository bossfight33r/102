"""Скоринг видео: ratio, robust z, velocity → score и reason_flags. Шортсы и длинные — раздельно."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta

from radar.config import AppConfig, ScoringConfig
from radar.db import Database
from radar.log import get_logger
from radar.schemas import (
    Channel,
    ChannelBaseline,
    ChannelStatus,
    Outlier,
    TaskResult,
    Video,
    VideoFormat,
    VideoSnapshot,
)
from radar.score.baseline import Sample, compute_baseline, pick_samples, ratio, robust_z
from radar.timeutil import age_days

# Интерполяция «просмотров в возрасте A» допустима, только если есть снимок не позже A·1.5 + 6 ч.
_BRACKET_FACTOR = 1.5
_BRACKET_PAD_H = 6.0


def views_at_age(
    published_at: datetime, snaps: Sequence[VideoSnapshot], age_h: float
) -> float | None:
    """Просмотры в возрасте age_h часов: линейная интерполяция между снимками (и точкой (0, 0))."""
    points = sorted(
        ((s.collected_at - published_at).total_seconds() / 3600.0, float(s.views)) for s in snaps
    )
    prev = (0.0, 0.0)
    for h, v in points:
        if h >= age_h:
            if h > age_h * _BRACKET_FACTOR + _BRACKET_PAD_H:
                return None
            if h == prev[0]:
                return v
            return prev[1] + (v - prev[1]) * (age_h - prev[0]) / (h - prev[0])
        prev = (h, v)
    return None


def velocity_ratio(
    video: Video,
    views: int,
    now: datetime,
    others: Sequence[tuple[Video, Sequence[VideoSnapshot]]],
    baseline: ChannelBaseline | None,
    cfg: ScoringConfig,
) -> tuple[float | None, str]:
    """Скорость набора против типичной для канала в том же возрасте.

    Возвращает (velocity_ratio, метод): "history" — по снимкам других видео канала,
    "prior" — против медианного видео, приведённого к возрасту вогнутой кривой
    median · sqrt(age / baseline_min_age_days) (только для видео младше baseline_min_age_days),
    "" — не считается.
    """
    age_d = age_days(video.published_at, now)
    if age_d > cfg.velocity_max_age_days:
        return None, ""
    age_h = age_d * 24
    typical = [
        v
        for o, snaps in others
        if (v := views_at_age(o.published_at, snaps, age_h)) is not None and v > 0
    ]
    if len(typical) >= cfg.velocity_min_videos:
        typical.sort()
        mid = len(typical) // 2
        med = typical[mid] if len(typical) % 2 else (typical[mid - 1] + typical[mid]) / 2
        return views / med, "history"
    if baseline and age_d < cfg.baseline_min_age_days:
        frac = math.sqrt(max(age_d, 0.25) / cfg.baseline_min_age_days)
        return views / (baseline.median_views * frac), "prior"
    return None, ""


def combine_score(
    r: float, z: float, vel: float | None, reliable: bool, cfg: ScoringConfig
) -> float:
    """score = w_r·log2(ratio) + w_z·z + w_v·log2(velocity); ×unreliable_factor при ненадёжном базлайне."""
    s = cfg.weight_ratio * math.log2(max(r, 1e-6)) + cfg.weight_z * z
    if vel is not None:
        s += cfg.weight_velocity * math.log2(max(vel, 1e-6))
    if not reliable and s > 0:
        s *= cfg.unreliable_factor
    return round(s, 4)


def reason_flags(
    *,
    r: float,
    z: float,
    vel: float | None,
    vel_method: str,
    views: int,
    subs: int | None,
    reliable: bool,
    age_d: float,
    cfg: ScoringConfig,
) -> list[str]:
    flags: list[str] = []
    if r >= cfg.flag_ratio:
        flags.append("high_ratio")
    if z >= cfg.flag_z:
        flags.append("high_z")
    if vel is not None and vel >= cfg.flag_velocity:
        flags.append("fast_start")
        if age_d < cfg.baseline_min_age_days:
            flags.append("early_signal")
    if subs:
        if views >= subs:
            flags.append("views_exceed_subs")
        if subs < cfg.small_channel_subs and views >= subs:
            flags.append("small_channel_breakout")
    if not reliable:
        flags.append("low_confidence")
    if vel_method == "prior":
        flags.append("velocity_fallback")
    return flags


def score_video(
    video: Video,
    views: int,
    now: datetime,
    mature: Sequence[Sample],
    others: Sequence[tuple[Video, Sequence[VideoSnapshot]]],
    channel: Channel,
    cfg: ScoringConfig,
) -> Outlier | None:
    """Оценка одного видео. None — нет базлайна. Порог аутлайера проверяет вызывающий код."""
    baseline = compute_baseline(
        channel.id, video.format, pick_samples(mature, cfg, exclude=video.id), cfg, now
    )
    if baseline is None:
        return None
    r = ratio(views, baseline)
    z = robust_z(views, baseline, cfg)
    vel, method = velocity_ratio(video, views, now, others, baseline, cfg)
    age_d = age_days(video.published_at, now)
    return Outlier(
        video_id=video.id,
        channel_id=channel.id,
        format=video.format,
        views=views,
        ratio=round(r, 3),
        z_score=round(z, 3),
        velocity_ratio=round(vel, 3) if vel is not None else None,
        score=combine_score(r, z, vel, baseline.reliable, cfg),
        reason_flags=reason_flags(
            r=r,
            z=z,
            vel=vel,
            vel_method=method,
            views=views,
            subs=channel.subs,
            reliable=baseline.reliable,
            age_d=age_d,
            cfg=cfg,
        ),
        detected_at=now,
    )


def is_outlier(o: Outlier, cfg: ScoringConfig) -> bool:
    return o.views >= cfg.min_views and o.score >= cfg.score_threshold


def score_all(db: Database, cfg: AppConfig, now: datetime) -> TaskResult:
    """Пересчёт базлайнов и скоринг отслеживаемых видео всех watching-каналов."""
    sc = cfg.scoring
    latest = db.latest_snapshots()
    track_after = now - timedelta(days=cfg.snapshots.mid_days)
    scored = found = 0
    found_ids: list[str] = []
    for channel in db.list_channels(status=ChannelStatus.WATCHING):
        videos = [v for v in db.list_videos(channel_id=channel.id) if v.id in latest]
        if not videos:
            continue
        snaps = db.snapshots_by_video([v.id for v in videos])
        by_format: dict[VideoFormat, list[Video]] = defaultdict(list)
        for v in videos:
            by_format[v.format].append(v)
        with db.tx():
            for fmt, vids in by_format.items():
                vids.sort(key=lambda v: v.published_at, reverse=True)
                mature = [
                    Sample(v.id, latest[v.id].views, age_days(v.published_at, now))
                    for v in vids
                    if age_days(v.published_at, now) >= sc.baseline_min_age_days
                ]
                full = compute_baseline(channel.id, fmt, pick_samples(mature, sc), sc, now)
                if full:
                    db.upsert_baseline(full)
                history = [(v, snaps.get(v.id, [])) for v in vids]
                for v in vids:
                    if v.published_at < track_after:
                        continue
                    others = [(o, s) for o, s in history if o.id != v.id]
                    o = score_video(v, latest[v.id].views, now, mature, others, channel, sc)
                    if o is None:
                        continue
                    scored += 1
                    if is_outlier(o, sc):
                        db.upsert_outlier(o, now)
                        found += 1
                        found_ids.append(v.id)
    log = get_logger(__name__)
    log.info("scoring_done", scored=scored, outliers=found)
    return TaskResult(
        name="score", stats={"scored": scored, "outliers": found}, message=",".join(found_ids)
    )
