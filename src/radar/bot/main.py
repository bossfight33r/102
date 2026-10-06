"""Процесс бота: radar bot (long polling)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from radar.bot.handlers import AdminOnlyMiddleware, build_router
from radar.log import get_logger

if TYPE_CHECKING:
    from radar.app import App

log = get_logger(__name__)

COMMANDS = [
    BotCommand(command="digest", description="Дайджест за сегодня"),
    BotCommand(command="outliers", description="Аутлайеры за 48 ч"),
    BotCommand(command="niches", description="Ниши"),
    BotCommand(command="candidates", description="Одобрение каналов"),
    BotCommand(command="quota", description="Квота YouTube API"),
    BotCommand(command="trends", description="Тренды за неделю"),
    BotCommand(command="analyze", description="Разобрать ролик по ссылке"),
    BotCommand(command="add", description="Добавить канал в watchlist"),
]


def build_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher(app=app)
    guard = AdminOnlyMiddleware(app.settings.admin_ids)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(build_router())
    return dp


async def watchdog(bot: Bot, app: App) -> None:
    """Фоновая проверка: tick давно не запускался → алерт админам (бот живёт отдельно от tick)."""
    from radar.app import current_time
    from radar.bot.handlers import stale_tick_alert

    while True:
        try:
            text = stale_tick_alert(app, current_time())
            if text:
                for chat_id in app.settings.admin_ids:
                    await bot.send_message(chat_id, text)
        except Exception as e:  # watchdog не должен ронять бота
            log.warning("watchdog_failed", error=type(e).__name__)
        await asyncio.sleep(app.config.bot.watchdog_check_minutes * 60)


async def _run(app: App, token: str) -> None:
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(app)
    await bot.set_my_commands(COMMANDS)
    log.info("bot_started", admins=len(app.settings.admin_ids))
    guard = asyncio.create_task(watchdog(bot, app)) if app.config.bot.watchdog_minutes else None
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if guard:
            guard.cancel()
        await bot.session.close()


def run_bot(app: App) -> None:
    from radar.app import ConfigError

    token = app.settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        raise ConfigError("TELEGRAM_BOT_TOKEN не задан")
    if not app.settings.admin_ids:
        raise ConfigError("ADMIN_IDS пуст: бот никому не ответит")
    asyncio.run(_run(app, token.get_secret_value()))
