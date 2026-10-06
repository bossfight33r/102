"""radar me: мой канал против моей медианы — та же математика скоринга, данные не сохраняются."""

from __future__ import annotations

from datetime import datetime

from radar.config import AppConfig
from radar.schemas import ChannelProfile, MyChannelReport, MyVideoScore, VideoFormat
from radar.score.baseline import Sample, compute_baseline, pick_samples
from radar.score.outliers import is_outlier, score_video
from radar.timeutil import age_days
from radar.youtube.client import YouTube


def my_channel_report(
    yt: YouTube, profile: ChannelProfile, cfg: AppConfig, now: datetime
) -> MyChannelReport:
    """3 ед. квоты: channels.list + playlistItems.list + videos.list (последние 50 роликов)."""
    if not profile.channel:
        raise ValueError("в config/channel_profile.yaml не задан channel (@handle моего канала)")
    ch = yt.resolve_channel(profile.channel, purpose="me", now=now)
    if ch is None:
        raise ValueError(f"канал {profile.channel} не найден")
    uploads = yt.list_uploads(ch.uploads_playlist_id, purpose="me", now=now, max_items=50)
    items = yt.get_videos([v for v, _ in uploads], purpose="me", now=now)
    sc = cfg.scoring
    report = MyChannelReport(channel_id=ch.id, channel_title=ch.title, subs=ch.subs)
    for fmt in VideoFormat:
        vids = sorted(
            ((v, st) for v, st in items if v.format == fmt),
            key=lambda p: p[0].published_at,
            reverse=True,
        )
        mature = [
            Sample(v.id, st.views, age_days(v.published_at, now))
            for v, st in vids
            if age_days(v.published_at, now) >= sc.baseline_min_age_days
        ]
        if baseline := compute_baseline(ch.id, fmt, pick_samples(mature, sc), sc, now):
            report.baselines.append(baseline)
        for v, st in vids:
            o = score_video(v, st.views, now, mature, [], ch, sc)
            report.videos.append(
                MyVideoScore(
                    video_id=v.id,
                    title=v.title,
                    format=fmt,
                    age_days=round(age_days(v.published_at, now), 1),
                    views=st.views,
                    ratio=o.ratio if o else None,
                    z_score=o.z_score if o else None,
                    score=o.score if o else None,
                    is_outlier=bool(o and is_outlier(o, sc)),
                )
            )
    report.videos.sort(key=lambda m: -(m.score if m.score is not None else -99))
    return report


def render_my_report(r: MyChannelReport, top: int = 10) -> str:
    from html import escape

    from radar.digest.render import human, join_limited

    lines = [f"📺 <b>{escape(r.channel_title)}</b> · подписчиков {human(r.subs or 0)}"]
    for b in r.baselines:
        note = "" if b.reliable else " (мало данных)"
        lines.append(
            f"Медиана {b.format.value}: {human(round(b.median_views))} по {b.sample_size} видео{note}"
        )
    hits = [v for v in r.videos if v.is_outlier]
    lines.append(f"\n<b>Мои аутлайеры</b>: {len(hits)}" if hits else "\nМоих аутлайеров нет.")
    lines.append("<b>Лучшие относительно медианы</b>")
    for v in r.videos[:top]:
        mark = "🔥 " if v.is_outlier else ""
        ratio = f"×{v.ratio:.1f}" if v.ratio is not None else "—"
        lines.append(
            f"{mark}{ratio} · {human(v.views)} · {v.age_days:g} дн. · {v.format.value} — "
            f'<a href="https://youtu.be/{v.video_id}">{escape(v.title)}</a>'
        )
    worst = [v for v in r.videos if v.ratio is not None][-3:]
    if worst and len(r.videos) > top:
        lines.append("<b>Слабее всего</b>")
        lines += [f"×{v.ratio:.2f} · {human(v.views)} — {escape(v.title)}" for v in worst]
    return join_limited(lines)
