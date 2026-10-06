"""Бот: логика команд и кнопок (синхронная, тестируемая) + aiogram-обвязка. Только ADMIN_IDS."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from html import escape
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, TelegramObject

from radar.bot.keyboards import CODE_ACTIONS, candidate_buttons, parse_callback, to_markup
from radar.digest.build import build_digest
from radar.digest.render import human, render_analysis, render_digest
from radar.export.suggestions import add_to_topics
from radar.log import get_logger
from radar.schemas import ChannelStatus, Feedback, FeedbackAction, OutMessage

if TYPE_CHECKING:
    from radar.app import App

log = get_logger(__name__)


# --- доступ -----------------------------------------------------------------------


def is_admin(user_id: int | None, admin_ids: Sequence[int]) -> bool:
    return user_id is not None and user_id in set(admin_ids)


class AdminOnlyMiddleware(BaseMiddleware):
    """Внешний middleware: апдейты не от ADMIN_IDS отбрасываются до хендлеров, без ответа."""

    def __init__(self, admin_ids: Sequence[int]) -> None:
        self.admin_ids = list(admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if not is_admin(getattr(user, "id", None), self.admin_ids):
            log.warning("bot_access_denied", user_id=getattr(user, "id", None))
            return None
        return await handler(event, data)


# --- логика (без aiogram) ------------------------------------------------------------


def handle_feedback(
    app: App, code: str, video_id: str, now: datetime
) -> tuple[str, list[OutMessage]]:
    """Нажатие кнопки карточки → запись Feedback + действие. Возвращает (toast, доп. сообщения)."""
    action = CODE_ACTIONS.get(code)
    if action is None:
        return "Неизвестная кнопка", []
    app.db.add_feedback(Feedback(video_id=video_id, action=action, at=now))
    if action == FeedbackAction.TO_TOPICS:
        ts = add_to_topics(app.db, video_id, app.profile, app.settings.exports_dir, now)
        return ("📌 Добавлено в темы" if ts else "Анализа ещё нет — тема не добавлена"), []
    if action == FeedbackAction.DETAILS:
        analysis = app.db.get_analysis(video_id)
        if analysis is None:
            return "Анализа ещё нет", []
        return "", [OutMessage(text=render_analysis(analysis, app.db.get_video(video_id)))]
    if action == FeedbackAction.HIDE_CHANNEL:
        video = app.db.get_video(video_id)
        if video:
            app.db.set_channel_status(video.channel_id, ChannelStatus.HIDDEN)
        return "🙈 Канал скрыт", []
    return "👎 Учтено", []


def handle_candidate(app: App, code: str, channel_id: str) -> str:
    status = {"a": ChannelStatus.WATCHING, "h": ChannelStatus.HIDDEN}.get(code)
    if status is None or not app.db.set_channel_status(channel_id, status):
        return "Канал не найден"
    return "✅ Следим" if status == ChannelStatus.WATCHING else "🙈 Скрыт"


def cmd_digest(app: App, now: datetime) -> list[OutMessage]:
    return render_digest(build_digest(app.db, app.config, now))


def cmd_outliers(app: App, now: datetime, limit: int = 10) -> str:
    rows = app.db.list_outliers(since=now - timedelta(hours=48), limit=limit)
    if not rows:
        return "Аутлайеров за 48 ч нет."
    lines = ["<b>Аутлайеры за 48 ч</b>"]
    for o in rows:
        v = app.db.get_video(o.video_id)
        title = escape(v.title if v else o.video_id)
        lines.append(
            f'{o.score:.1f} · ×{o.ratio:.1f} · {human(o.views)} — <a href="https://youtu.be/{o.video_id}">{title}</a>'
        )
    return "\n".join(lines)


def cmd_niches(app: App) -> str:
    niches = app.db.list_niches()
    if not niches:
        return "Ниш нет."
    lines = ["<b>Ниши</b>"]
    for n in niches:
        watching = len(
            [c for c in app.db.list_channels(status=ChannelStatus.WATCHING, niche_id=n.id)]
        )
        cands = len(app.db.list_channels(status=ChannelStatus.CANDIDATE, niche_id=n.id))
        state = "" if n.enabled else " (выкл)"
        lines.append(
            f"• {escape(n.name)}{state}: каналов {watching}, кандидатов {cands}, search/день {n.discovery_per_day}"
        )
    return "\n".join(lines)


def cmd_candidates(app: App, limit: int = 10) -> list[OutMessage]:
    cands = app.db.list_channels(status=ChannelStatus.CANDIDATE)
    if not cands:
        return [OutMessage(text="Кандидатов нет.")]
    msgs = [OutMessage(text=f"Кандидатов: {len(cands)} (показываю {min(limit, len(cands))})")]
    for c in cands[:limit]:
        handle = f" {escape(c.handle)}" if c.handle else ""
        msgs.append(
            OutMessage(
                text=f"<b>{escape(c.title)}</b>{handle}\nподписчиков: {human(c.subs or 0)}\n"
                f"https://www.youtube.com/channel/{c.id}",
                buttons=candidate_buttons(c.id),
            )
        )
    return msgs


def cmd_quota(app: App, now: datetime) -> str:
    st = app.planner.status(now)
    text = (
        f"Квота {st['date']} (PT): <b>{st['used']}</b>/{st['budget']} ед.\n"
        f"discovery {st['discovery_used']}/{st['discovery_reserve']} (резерв)"
    )
    if st["paused_until"]:
        text += f"\n⏸ пауза до {st['paused_until']}"
    return text


def cmd_trends(app: App, now: datetime) -> str:
    from radar.trends import build_trends, render_trends_text

    return render_trends_text(build_trends(app.db, app.config, now, days=app.config.trends.days))[
        :4096
    ]


# --- aiogram -------------------------------------------------------------------------


async def send_out(message: Message, msgs: Sequence[OutMessage]) -> None:
    for m in msgs:
        markup = to_markup(m.buttons) if m.buttons else None
        if m.photo_url and len(m.text) <= 1024:
            try:
                await message.answer_photo(m.photo_url, caption=m.text, reply_markup=markup)
                continue
            except Exception as e:  # битое превью — шлём текстом
                log.info("photo_failed", error=type(e).__name__)
        await message.answer(m.text[:4096], reply_markup=markup)


def _now() -> datetime:
    from radar.app import current_time

    return current_time()


async def on_help(message: Message, app: App) -> None:
    await message.answer(
        "Outlier Radar. Команды:\n/digest — дайджест за сегодня\n/outliers — аутлайеры за 48 ч\n"
        "/niches — ниши\n/candidates — одобрение каналов\n/quota — квота API\n/trends — тренды за неделю"
    )


async def on_digest(message: Message, app: App) -> None:
    await send_out(message, cmd_digest(app, _now()))


async def on_outliers(message: Message, app: App) -> None:
    await message.answer(cmd_outliers(app, _now()), disable_web_page_preview=True)


async def on_niches(message: Message, app: App) -> None:
    await message.answer(cmd_niches(app))


async def on_candidates(message: Message, app: App) -> None:
    await send_out(message, cmd_candidates(app))


async def on_quota(message: Message, app: App) -> None:
    await message.answer(cmd_quota(app, _now()))


async def on_trends(message: Message, app: App) -> None:
    await message.answer(cmd_trends(app, _now()))


async def on_feedback(callback: CallbackQuery, app: App) -> None:
    parsed = parse_callback(callback.data or "")
    if parsed is None:
        await callback.answer("Некорректные данные")
        return
    _, code, video_id = parsed
    toast, extra = handle_feedback(app, code, video_id, _now())
    await callback.answer(toast or None)
    if extra and isinstance(callback.message, Message):
        await send_out(callback.message, extra)


async def on_candidate(callback: CallbackQuery, app: App) -> None:
    parsed = parse_callback(callback.data or "")
    if parsed is None:
        await callback.answer("Некорректные данные")
        return
    _, code, channel_id = parsed
    await callback.answer(handle_candidate(app, code, channel_id))


def build_router() -> Router:
    """Новый Router на каждый Dispatcher (aiogram не даёт подключать один Router дважды)."""
    r = Router(name="radar")
    r.message.register(on_help, Command("start", "help"))
    r.message.register(on_digest, Command("digest"))
    r.message.register(on_outliers, Command("outliers"))
    r.message.register(on_niches, Command("niches"))
    r.message.register(on_candidates, Command("candidates"))
    r.message.register(on_quota, Command("quota"))
    r.message.register(on_trends, Command("trends"))
    r.callback_query.register(on_feedback, F.data.startswith("fb:"))
    r.callback_query.register(on_candidate, F.data.startswith("ch:"))
    return r
