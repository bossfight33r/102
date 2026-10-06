"""Рендер дайджеста и анализа в сообщения Telegram (HTML)."""

from __future__ import annotations

from html import escape

from radar.bot.keyboards import feedback_buttons
from radar.schemas import Analysis, Digest, DigestItem, OutMessage, Video

FLAG_LABELS = {
    "high_ratio": "×медиана",
    "high_z": "редкий выброс",
    "fast_start": "быстрый старт",
    "early_signal": "ранний сигнал",
    "small_channel_breakout": "прорыв малого канала",
    "views_exceed_subs": "просмотры > подписчиков",
    "low_confidence": "мало данных",
}


def human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн"
    if n >= 1_000:
        return f"{n / 1_000:.1f} тыс"
    return str(n)


def render_card(item: DigestItem) -> OutMessage:
    flags = ", ".join(FLAG_LABELS[f] for f in item.reason_flags if f in FLAG_LABELS)
    lines = [
        f"<b>{escape(item.title)}</b>",
        f"{escape(item.channel_title)} · <b>×{item.ratio:.1f}</b> · {human(item.views)} просм. · "
        f"{item.age_days:g} дн. · {'shorts' if item.format.value == 'short' else 'long'}",
    ]
    if flags:
        lines.append(f"<i>{escape(flags)}</i>")
    lines.append(f"💡 {escape(item.why_short)}" if item.why_short else "💡 анализ ещё не готов")
    if item.idea:
        lines.append(f"🎯 Идея: {escape(item.idea)}")
    lines.append(item.url)
    return OutMessage(
        text="\n".join(lines), photo_url=item.thumbnail_url, buttons=feedback_buttons(item.video_id)
    )


def render_digest(digest: Digest, intro: str | None = None) -> list[OutMessage]:
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
        msgs.append(render_card(item))
    return msgs


def render_analysis(analysis: Analysis, video: Video | None) -> str:
    w = analysis.why_it_worked
    idea = analysis.idea_for_my_channel
    title = escape(video.title) if video else analysis.video_id
    parts = [
        f"🔎 <b>{title}</b>",
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
    return "\n".join(parts)
