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
]


def build_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher(app=app)
    guard = AdminOnlyMiddleware(app.settings.admin_ids)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(build_router())
    return dp


async def _run(app: App, token: str) -> None:
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(app)
    await bot.set_my_commands(COMMANDS)
    log.info("bot_started", admins=len(app.settings.admin_ids))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


def run_bot(app: App) -> None:
    from radar.app import ConfigError

    token = app.settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        raise ConfigError("TELEGRAM_BOT_TOKEN не задан")
    if not app.settings.admin_ids:
        raise ConfigError("ADMIN_IDS пуст: бот никому не ответит")
    asyncio.run(_run(app, token.get_secret_value()))
