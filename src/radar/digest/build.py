"""Сборка дайджеста: топ-N свежих аутлайеров по нишам. Один дайджест на локальную дату."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from radar.bot.notifier import Notifier
from radar.config import AppConfig
from radar.db import Database
from radar.llm.base import LLMError, LLMProvider
from radar.log import get_logger
from radar.schemas import (
    ChannelProfile,
    ChannelStatus,
    Digest,
    DigestItem,
    FeedbackAction,
    FormatPref,
    Niche,
    Outlier,
    TaskResult,
    VideoFormat,
)
from radar.timeutil import age_days, ensure_utc

log = get_logger(__name__)


def local_date(now: datetime, cfg: AppConfig) -> date:
    return ensure_utc(now).astimezone(ZoneInfo(cfg.digest.timezone)).date()


def _fits(niche: Niche, fmt: VideoFormat) -> bool:
    if niche.format == FormatPref.SHORTS:
        return fmt == VideoFormat.SHORT
    if niche.format == FormatPref.LONG:
        return fmt == VideoFormat.LONG
    return True


def _short(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_item(db: Database, o: Outlier, niche: Niche, now: datetime) -> DigestItem | None:
    video = db.get_video(o.video_id)
    channel = db.get_channel(o.channel_id)
    if video is None or channel is None:
        return None
    analysis = db.get_analysis(o.video_id)
    why = idea = None
    if analysis:
        w = analysis.why_it_worked
        why = _short(f"{w.title_pattern}. {w.topic}")
        idea = _short(analysis.idea_for_my_channel.title)
    return DigestItem(
        video_id=o.video_id,
        niche_id=niche.id,
        niche_name=niche.name,
        title=video.title,
        channel_title=channel.title,
        url=f"https://youtu.be/{o.video_id}",
        thumbnail_url=video.thumbnail_url,
        format=o.format,
        ratio=o.ratio,
        views=o.views,
        age_days=round(age_days(video.published_at, now), 1),
        score=o.score,
        reason_flags=o.reason_flags,
        why_short=why,
        idea=idea,
    )


def select_items(db: Database, cfg: AppConfig, now: datetime, day: date) -> list[DigestItem]:
    excluded = db.digested_video_ids(exclude_day=day)
    excluded |= {f.video_id for f in db.list_feedback(FeedbackAction.NOT_RELEVANT)}
    niches = {n.id: n for n in db.list_niches(enabled_only=True)}
    per_niche: dict[str, list[DigestItem]] = defaultdict(list)
    for o in db.list_outliers(since=now - timedelta(hours=cfg.digest.lookback_hours)):
        if o.video_id in excluded:
            continue
        channel = db.get_channel(o.channel_id)
        if channel is None or channel.status != ChannelStatus.WATCHING:
            continue
        niche = next(
            (niches[n] for n in channel.niche_ids if n in niches and _fits(niches[n], o.format)),
            None,
        )
        if niche is None:
            continue
        if len(per_niche[niche.id]) >= cfg.digest.top_n_per_niche:
            continue
        item = make_item(db, o, niche, now)
        if item:
            per_niche[niche.id].append(item)
    items = sorted((i for lst in per_niche.values() for i in lst), key=lambda i: -i.score)
    items = items[: cfg.digest.max_items]
    # группировка по нишам в порядке лучшего score ниши
    order = list(dict.fromkeys(i.niche_id for i in items))
    return sorted(items, key=lambda i: (order.index(i.niche_id), -i.score))


def build_digest(db: Database, cfg: AppConfig, now: datetime, *, rebuild: bool = False) -> Digest:
    """Дайджест на локальную дату. Уже собранный возвращается как есть (отправленный не пересобирается)."""
    day = local_date(now, cfg)
    existing = db.get_digest(day)
    if existing and (existing.sent_at or not rebuild):
        return existing
    digest = Digest(date=day, items=select_items(db, cfg, now, day))
    db.save_digest(digest)
    return digest


def digest_due(db: Database, cfg: AppConfig, now: datetime) -> bool:
    local = ensure_utc(now).astimezone(ZoneInfo(cfg.digest.timezone))
    if local.hour < cfg.digest.send_hour:
        return False
    d = db.get_digest(local.date())
    return d is None or d.sent_at is None


def llm_intro(
    digest: Digest, llm: LLMProvider, profile: ChannelProfile | None, db: Database, now: datetime
) -> str | None:
    from radar.analyze.analyzer import load_prompt

    payload = {
        "outliers": [
            {"niche": i.niche_name, "title": i.title, "ratio": i.ratio, "why": i.why_short}
            for i in digest.items
        ],
        "my_channel": profile.model_dump(mode="json") if profile else None,
    }
    try:
        resp = llm.complete(
            system=load_prompt("digest"),
            prompt=json.dumps(payload, ensure_ascii=False),
            max_tokens=600,
        )
    except LLMError as e:
        log.warning("digest_intro_failed", error=str(e)[:200])
        return None
    db.add_llm_usage(
        "digest_intro", resp.model, resp.input_tokens, resp.output_tokens, resp.cost, now
    )
    return resp.text.strip()[:800] or None


def send_digest(
    db: Database,
    cfg: AppConfig,
    notifier: Notifier,
    now: datetime,
    *,
    llm: LLMProvider | None = None,
    profile: ChannelProfile | None = None,
) -> TaskResult:
    """Собрать (если нужно) и отправить дайджест за локальную дату. Повторно не отправляет."""
    from radar.digest.render import render_digest

    digest = build_digest(db, cfg, now)
    if digest.sent_at:
        return TaskResult(
            name="digest", stats={"items": len(digest.items), "sent": 0}, message="уже отправлен"
        )
    intro = (
        llm_intro(digest, llm, profile, db, now)
        if (llm and cfg.digest.use_llm_intro and digest.items)
        else None
    )
    delivered = notifier.send(render_digest(digest, intro))
    db.save_digest(digest.model_copy(update={"sent_at": now}))
    return TaskResult(name="digest", stats={"items": len(digest.items), "sent": delivered})
