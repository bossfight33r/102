"""Рендер дайджеста и анализа в сообщения Telegram (HTML)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import escape, unescape

from radar.bot.keyboards import feedback_buttons
from radar.db import Database
from radar.schemas import (
    Analysis,
    ChannelBaseline,
    Digest,
    DigestItem,
    Outlier,
    OutMessage,
    Video,
    VideoSnapshot,
)

FLAG_LABELS = {
    "high_ratio": "×медиана",
    "high_z": "редкий выброс",
    "fast_start": "быстрый старт",
    "early_signal": "ранний сигнал",
    "small_channel_breakout": "прорыв малого канала",
    "views_exceed_subs": "просмотры > подписчиков",
    "low_confidence": "мало данных",
}


def plain(html_text: str) -> str:
    """HTML Telegram → обычный текст (для консоли)."""
    html_text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", html_text)
    return unescape(re.sub(r"<[^>]+>", "", html_text))


def human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн"
    if n >= 1_000:
        return f"{n / 1_000:.1f} тыс"
    return str(n)


def render_card(item: DigestItem, with_analysis: bool = True) -> OutMessage:
    flags = ", ".join(FLAG_LABELS[f] for f in item.reason_flags if f in FLAG_LABELS)
    lines = [
        f"<b>{escape(item.title)}</b>",
        f"{escape(item.channel_title)} · <b>×{item.ratio:.1f}</b> · {human(item.views)} просм. · "
        f"{item.age_days:g} дн. · {'shorts' if item.format.value == 'short' else 'long'}",
    ]
    if flags:
        lines.append(f"<i>{escape(flags)}</i>")
    if item.why_short:
        lines.append(f"💡 {escape(item.why_short)}")
    elif with_analysis:
        lines.append("💡 анализ ещё не готов")
    if item.idea:
        lines.append(f"🎯 Идея: {escape(item.idea)}")
    lines.append(item.url)
    return OutMessage(
        text="\n".join(lines), photo_url=item.thumbnail_url, buttons=feedback_buttons(item.video_id)
    )


def render_digest(
    digest: Digest, intro: str | None = None, with_analysis: bool = True
) -> list[OutMessage]:
    if not digest.items:
        return [OutMessage(text=f"📭 Дайджест {digest.date:%d.%m}: новых аутлайеров нет.")]
    niches = list(dict.fromkeys(i.niche_name for i in digest.items))
    head = [
        f"📡 <b>Дайджест {digest.date:%d.%m}</b>: {len(digest.items)} аутлайеров, ниши: {escape(', '.join(niches))}"
    ]
    if intro:
        head.append(escape(intro))
    msgs = [OutMessage(text="\n\n".join(head))]
    current = None
    for item in digest.items:
        if item.niche_name != current and len(niches) > 1:
            msgs.append(OutMessage(text=f"— <b>{escape(item.niche_name)}</b> —"))
        current = item.niche_name
        msgs.append(render_card(item, with_analysis))
    return msgs


TELEGRAM_LIMIT = 4096
SPARK = "▁▂▃▄▅▆▇█"


def join_limited(lines: list[str], limit: int = TELEGRAM_LIMIT) -> str:
    """Склейка целых строк в пределах limit: обрезка посреди HTML-тега ломает разбор в Telegram."""
    out: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if out else 0)
        if size + add > limit - 2:
            out.append("…")
            break
        out.append(line)
        size += add
    return "\n".join(out)


def sparkline(values: list[int], width: int = 16) -> str:
    """Текстовый график прироста просмотров: ▁▂▃…█ по равномерной выборке точек."""
    if len(values) < 2:
        return ""
    if len(values) > width:
        step = (len(values) - 1) / (width - 1)
        values = [values[round(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi == lo:
        return SPARK[0] * len(values)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


def render_metrics(
    outlier: Outlier | None,
    baseline: ChannelBaseline | None,
    snapshots: list[VideoSnapshot],
) -> list[str]:
    """Почему ролик считается аутлайером: цифры скоринга и динамика."""
    lines: list[str] = []
    if outlier:
        vel = f" · скорость ×{outlier.velocity_ratio:.1f}" if outlier.velocity_ratio else ""
        lines.append(
            f"📊 score <b>{outlier.score:.1f}</b> · ×{outlier.ratio:.1f} к медиане · z {outlier.z_score:.1f}{vel}"
        )
        flags = ", ".join(FLAG_LABELS.get(f, f) for f in outlier.reason_flags)
        if flags:
            lines.append(f"<i>{escape(flags)}</i>")
    if baseline:
        note = "" if baseline.reliable else ", мало данных"
        lines.append(
            f"Медиана канала ({baseline.format.value}): {human(round(baseline.median_views))} "
            f"по {baseline.sample_size} видео{note}"
        )
    if len(snapshots) >= 2:
        views = [s.views for s in snapshots]
        lines.append(f"Просмотры: {sparkline(views)} {human(views[0])} → {human(views[-1])}")
    return lines


def render_analysis(
    analysis: Analysis, video: Video | None, metrics: list[str] | None = None
) -> str:
    w = analysis.why_it_worked
    idea = analysis.idea_for_my_channel
    title = escape(video.title) if video else analysis.video_id
    parts = [
        f"🔎 <b>{title}</b>",
        *(metrics or []),
        "<b>Почему зашло</b>",
        f"• Заголовок: {escape(w.title_pattern)}",
        f"• Превью: {escape(', '.join(w.thumbnail_elements) or '—')}",
        f"• Тема: {escape(w.topic)}",
        f"• Формат: {escape(w.format)}",
        f"• Длительность: {escape(w.duration)}",
        f"<b>Хук</b>: {escape(analysis.hook_formula)}",
    ]
    if analysis.audience_questions:
        parts.append(
            "<b>Вопросы зрителей</b>\n"
            + "\n".join(f"• {escape(q)}" for q in analysis.audience_questions)
        )
    parts += [
        f"<b>Идея для моего канала</b>: {escape(idea.title)}",
        escape(idea.pitch),
        f"<i>Отличие от оригинала</i>: {escape(idea.difference_from_original)}",
        *(f"• {escape(p)}" for p in idea.key_points),
        f"<b>Shorts</b>: {escape(analysis.short_form_angle)}",
        f"<i>уверенность {analysis.confidence:.2f} · {escape(analysis.model)} · ${analysis.cost:.4f}</i>",
    ]
    return join_limited(parts)


def render_digest_markdown(digest: Digest, intro: str | None, db: Database | None = None) -> str:
    """Markdown-архив дайджеста: карточки + полный анализ (если есть)."""
    out = [f"# Дайджест {digest.date.isoformat()}", ""]
    if intro:
        out += [intro, ""]
    if not digest.items:
        out.append("Новых аутлайеров нет.")
    current = None
    for i in digest.items:
        if i.niche_name != current:
            out += [f"## {i.niche_name}", ""]
            current = i.niche_name
        out += [
            f"### [{i.title}]({i.url})",
            f"{i.channel_title} · ×{i.ratio:.1f} · {human(i.views)} просм. · {i.age_days:g} дн. · "
            f"{i.format.value} · score {i.score:.1f}",
            "",
        ]
        analysis = db.get_analysis(i.video_id) if db else None
        if analysis:
            w, idea = analysis.why_it_worked, analysis.idea_for_my_channel
            out += [
                f"- **Почему зашло:** {w.title_pattern}; {w.topic}; {w.format}; {w.duration}",
                f"- **Хук:** {analysis.hook_formula}",
                f"- **Идея:** {idea.title} — {idea.pitch}",
                f"- **Отличие от оригинала:** {idea.difference_from_original}",
                *(f"  - {p}" for p in idea.key_points),
                f"- **Shorts:** {analysis.short_form_angle}",
            ]
            if analysis.audience_questions:
                out.append("- **Вопросы зрителей:** " + "; ".join(analysis.audience_questions))
        elif i.why_short:
            out.append(f"- {i.why_short}")
        out.append("")
    return "\n".join(out)


def details_text(db: Database, video_id: str, analysis: Analysis | None) -> str:
    """Полный Analysis + цифры скоринга и спарклайн (бот «Подробнее», radar analyze).
    Без анализа (режим без LLM) — только цифры."""
    video = db.get_video(video_id)
    baseline = db.get_baseline(video.channel_id, video.format) if video else None
    metrics = render_metrics(db.get_outlier(video_id), baseline, db.snapshots_for(video_id))
    if analysis is None:
        title = escape(video.title) if video else video_id
        return join_limited([f"🔎 <b>{title}</b>", *metrics, f"https://youtu.be/{video_id}"])
    return render_analysis(analysis, video, metrics)


def llm_spend_line(db: Database, limit_usd: float, now: datetime) -> str:
    day = db.llm_cost_since(now - timedelta(hours=24))
    week = db.llm_cost_since(now - timedelta(days=7))
    return f"LLM: ${day:.2f} за 24 ч (лимит ${limit_usd:.2f}), ${week:.2f} за 7 дней"
