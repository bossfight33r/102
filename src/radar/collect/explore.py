"""Популярное на YouTube и поиск ниш под свои видео. Только метаданные из официального API."""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from radar.config import AppConfig
from radar.db import Database
from radar.llm.base import LLMProvider, extract_json
from radar.schemas import (
    Channel,
    ChannelProfile,
    ExampleVideo,
    NicheScore,
    PopularVideo,
    Video,
    VideoFormat,
    VideoStats,
)
from radar.timeutil import age_days
from radar.youtube.client import QuotaExceededError, YouTube
from radar.youtube.quota import QuotaDeferred

if TYPE_CHECKING:
    from radar.app import App

EXPLORE_PURPOSE = "discovery:explore"  # делит с discovery резерв квоты, не трогает watchlist
MIN_AGE_DAYS = 0.5  # свежее — views/day слишком шумные


def _rows(
    items: list[tuple[Video, VideoStats]], channels: dict[str, Channel], now: datetime
) -> list[PopularVideo]:
    out: list[PopularVideo] = []
    for v, st in items:
        ch = channels.get(v.channel_id)
        subs = ch.subs if ch else None
        age = age_days(v.published_at, now)
        out.append(
            PopularVideo(
                video_id=v.id,
                title=v.title,
                channel_id=v.channel_id,
                channel_title=ch.title if ch else "",
                subs=subs,
                format=v.format,
                duration_sec=v.duration_sec,
                age_days=round(age, 1),
                views=st.views,
                likes=st.likes,
                comments=st.comments,
                views_per_day=round(st.views / max(age, MIN_AGE_DAYS), 1),
                like_rate=round(st.likes / st.views, 4)
                if st.likes is not None and st.views
                else None,
                subs_ratio=round(st.views / subs, 2) if subs else None,
                url=f"https://youtu.be/{v.id}",
                thumbnail_url=v.thumbnail_url,
            )
        )
    return out


def _channels_for(
    yt: YouTube, items: list[tuple[Video, VideoStats]], now: datetime
) -> dict[str, Channel]:
    ids = sorted({v.channel_id for v, _ in items})
    return {c.id: c for c in yt.get_channels(ids, purpose=EXPLORE_PURPOSE, now=now)} if ids else {}


def popular_chart(
    yt: YouTube,
    cfg: AppConfig,
    now: datetime,
    *,
    region: str,
    category_id: str | None = None,
    fmt: VideoFormat | None = None,
) -> list[PopularVideo]:
    """Официальный топ YouTube по региону (1 ед. + 1 ед. на подписчиков каналов)."""
    items = yt.most_popular(purpose="popular", now=now, region_code=region, category_id=category_id)
    if fmt:
        items = [(v, s) for v, s in items if v.format == fmt]
    return sorted(_rows(items, _channels_for(yt, items, now), now), key=lambda r: -r.views_per_day)[
        : cfg.explore.popular_limit
    ]


def search_videos(
    yt: YouTube,
    query: str,
    cfg: AppConfig,
    now: datetime,
    *,
    days: int,
    region: str | None,
    language: str | None,
    fmt: VideoFormat | None = None,
    purpose: str = EXPLORE_PURPOSE,
) -> list[tuple[Video, VideoStats]]:
    """search.list (100 ед.) → videos.list со статистикой (1 ед.)."""
    hits = yt.search(
        query,
        purpose=purpose,
        now=now,
        published_after=now - timedelta(days=days),
        region_code=region,
        relevance_language=language,
        video_duration="short" if fmt == VideoFormat.SHORT else None,
        max_results=50,
    )
    items = yt.get_videos([h.video_id for h in hits], purpose=purpose, now=now)
    if fmt:
        items = [(v, s) for v, s in items if v.format == fmt]
    return items


def popular_search(
    yt: YouTube,
    query: str,
    cfg: AppConfig,
    now: datetime,
    *,
    days: int,
    region: str | None,
    language: str | None,
    fmt: VideoFormat | None = None,
) -> list[PopularVideo]:
    """Самые просматриваемые по теме за период, по скорости набора просмотров."""
    items = search_videos(
        yt, query, cfg, now, days=days, region=region, language=language, fmt=fmt, purpose="popular"
    )
    rows = _rows(items, _channels_for(yt, items, now), now)
    return sorted(rows, key=lambda r: -r.views_per_day)[: cfg.explore.popular_limit]


def score_niche(topic: str, rows: list[PopularVideo], cfg: AppConfig) -> NicheScore:
    """Оценка ниши по свежим популярным роликам темы.

    score = log10(1 + медианные просмотры/сутки) · (0.3 + breakout_share) · (1 − 0.5·big_share)
    спрос × шанс малого канала × поправка на засилье крупных каналов.
    """
    ex = cfg.explore
    if not rows:
        return NicheScore(
            topic=topic,
            n_videos=0,
            median_views_per_day=0,
            median_views=0,
            small_channel_share=0,
            breakout_share=0,
            big_channel_share=0,
            shorts_share=0,
            score=0,
            note="ничего не найдено",
        )
    known = [r for r in rows if r.subs]
    n_known = len(known) or 1
    small = [r for r in known if (r.subs or 0) < ex.small_channel_subs]
    breakout = [r for r in known if r.subs_ratio and r.subs_ratio >= 1]
    big = [r for r in known if (r.subs or 0) > ex.big_channel_subs]
    mvpd = statistics.median(r.views_per_day for r in rows)
    score = math.log10(1 + mvpd) * (0.3 + len(breakout) / n_known) * (1 - 0.5 * len(big) / n_known)
    notes = []
    if len(rows) < 10:
        notes.append("мало роликов — оценка грубая")
    if len(small) / n_known >= 0.4 and len(breakout) / n_known >= 0.3:
        notes.append("малые каналы выстреливают")
    if len(big) / n_known >= 0.6:
        notes.append("тему держат крупные каналы")
    best = sorted(rows, key=lambda r: -(r.subs_ratio or 0))[: ex.examples]
    return NicheScore(
        topic=topic,
        n_videos=len(rows),
        median_views_per_day=round(mvpd, 1),
        median_views=statistics.median(r.views for r in rows),
        small_channel_share=round(len(small) / n_known, 2),
        breakout_share=round(len(breakout) / n_known, 2),
        big_channel_share=round(len(big) / n_known, 2),
        shorts_share=round(sum(r.format == VideoFormat.SHORT for r in rows) / len(rows), 2),
        score=round(score, 3),
        examples=[
            ExampleVideo(
                title=r.title, url=r.url, views=r.views, channel_title=r.channel_title, subs=r.subs
            )
            for r in best
        ],
        note="; ".join(notes),
    )


def explore_topics(
    yt: YouTube,
    topics: list[str],
    cfg: AppConfig,
    now: datetime,
    *,
    region: str | None,
    language: str | None,
    days: int | None = None,
    fmt: VideoFormat | None = None,
) -> list[NicheScore]:
    """Рейтинг тем по убыванию score. Остановка при нехватке квоты — отдаёт уже посчитанное."""
    days = days or cfg.explore.days
    out: list[NicheScore] = []
    for topic in topics[: cfg.explore.max_topics]:
        try:
            items = search_videos(
                yt, topic, cfg, now, days=days, region=region, language=language, fmt=fmt
            )
            rows = _rows(items, _channels_for(yt, items, now), now)
        except (QuotaDeferred, QuotaExceededError):
            break
        out.append(score_niche(topic, rows, cfg))
    return sorted(out, key=lambda n: -n.score)


def explore_cost(cfg: AppConfig, n_topics: int) -> int:
    """Оценка квоты: search + videos.list + channels.list на тему."""
    c = cfg.quota.costs
    return min(n_topics, cfg.explore.max_topics) * (
        c["search.list"] + c["videos.list"] + c["channels.list"]
    )


def expand_topic(
    llm: LLMProvider,
    topic: str,
    profile: ChannelProfile | None,
    cfg: AppConfig,
    db: Database,
    now: datetime,
) -> list[str]:
    """Широкая тема → узкие поисковые запросы через LLM (стоимость пишется в llm_usage)."""
    from radar.analyze.analyzer import load_prompt

    payload = {
        "topic": topic,
        "count": cfg.explore.expand_count,
        "my_channel": profile.model_dump(mode="json", exclude={"channel"}) if profile else None,
    }
    resp = llm.complete(
        system=load_prompt("explore"),
        prompt=json.dumps(payload, ensure_ascii=False),
        max_tokens=cfg.llm.light_max_tokens,
        effort=cfg.llm.light_effort,
        json_mode=True,
    )
    db.add_llm_usage("explore", resp.model, resp.input_tokens, resp.output_tokens, resp.cost, now)
    raw = extract_json(resp.text).get("topics", [])
    out: list[str] = []
    for t in raw:
        t = " ".join(str(t).split())[:80]
        if t and t.lower() not in {x.lower() for x in out} and t.lower() != topic.lower():
            out.append(t)
    return out[: cfg.explore.expand_count]


# --- общая логика CLI и бота ----------------------------------------------------------

NICHE_FILE = "niche_explore.yaml"


def default_region_language(app: App) -> tuple[str, str]:
    """Регион и язык по умолчанию — из первой включённой ниши, иначе RU/ru."""
    for n in app.db.list_niches(enabled_only=True):
        return n.region or "RU", n.language or "ru"
    return "RU", (app.profile.language if app.profile else "ru")


def save_explore(app: App, scores: list[NicheScore], now: datetime, days: int) -> Path:
    from radar.export.suggestions import write_yaml
    from radar.timeutil import iso

    path = app.settings.exports_dir / NICHE_FILE
    write_yaml(
        path,
        {
            "generated_at": iso(now),
            "days": days,
            "niches": [s.model_dump(mode="json") for s in scores],
        },
    )
    return path


def run_explore(
    app: App,
    topics: list[str],
    now: datetime,
    *,
    region: str | None = None,
    language: str | None = None,
    days: int | None = None,
    fmt: VideoFormat | None = None,
) -> tuple[list[NicheScore], int]:
    """Оценка тем + запись data/exports/niche_explore.yaml. Возвращает (рейтинг, окно в днях)."""
    d_region, d_lang = default_region_language(app)
    days = days or app.config.explore.days
    scores = explore_topics(
        app.youtube,
        topics,
        app.config,
        now,
        region=region or d_region,
        language=language or d_lang,
        days=days,
        fmt=fmt,
    )
    save_explore(app, scores, now, days)
    return scores, days
