"""Notifier: доставка сообщений админам. Telegram — реальная реализация, Fake — для тестов."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from radar.log import get_logger
from radar.schemas import OutMessage

log = get_logger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
FLOOD_RETRIES = 3
MAX_FLOOD_WAIT = 60


class Notifier(Protocol):
    def send(self, messages: Sequence[OutMessage]) -> int:
        """Отправить сообщения всем админам. Возвращает число доставленных сообщений."""
        ...


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[OutMessage] = []

    def send(self, messages: Sequence[OutMessage]) -> int:
        self.sent.extend(messages)
        return len(messages)


class ConsoleNotifier:
    """Демо-режим (RADAR_FAKE): печатает сообщения в stdout вместо Telegram."""

    def send(self, messages: Sequence[OutMessage]) -> int:
        from radar.digest.render import plain

        for m in messages:
            print(plain(m.text))
            if m.buttons:
                print("  [" + "] [".join(b.text for row in m.buttons for b in row) + "]")
            print()
        return len(messages)


class TelegramNotifier:
    def __init__(self, token: str, admin_ids: Sequence[int], session: Any = None) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан")
        if not admin_ids:
            raise ValueError("ADMIN_IDS пуст: некому отправлять")
        self._token = token
        self.admin_ids = list(admin_ids)
        self._session = session  # тесты подставляют фейковую aiogram-сессию
        self._sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep

    def send(self, messages: Sequence[OutMessage]) -> int:
        return asyncio.run(self._send_all(messages))

    async def _flood_safe(self, call: Callable[[], Awaitable[Any]]) -> Any:
        """Telegram ограничивает частоту (≈1 сообщение/с в чат): при RetryAfter ждём и повторяем."""
        from aiogram.exceptions import TelegramRetryAfter

        for attempt in range(FLOOD_RETRIES + 1):
            try:
                return await call()
            except TelegramRetryAfter as e:
                if attempt == FLOOD_RETRIES:
                    raise
                log.info("telegram_flood_wait", seconds=e.retry_after)
                await self._sleep(min(e.retry_after, MAX_FLOOD_WAIT))
        return None

    async def _send_all(self, messages: Sequence[OutMessage]) -> int:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from radar.bot.keyboards import to_markup

        delivered = 0
        bot = Bot(
            self._token,
            session=self._session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        try:
            for chat_id in self.admin_ids:
                for m in messages:
                    markup = to_markup(m.buttons) if m.buttons else None
                    try:
                        if m.photo_url and len(m.text) <= CAPTION_LIMIT:
                            await self._flood_safe(
                                lambda c=chat_id, m=m, k=markup: bot.send_photo(
                                    c, m.photo_url, caption=m.text, reply_markup=k
                                )
                            )
                        else:
                            await self._flood_safe(
                                lambda c=chat_id, m=m, k=markup: bot.send_message(
                                    c, m.text[:TEXT_LIMIT], reply_markup=k
                                )
                            )
                        delivered += 1
                    except Exception as e:  # одна битая карточка не валит весь дайджест
                        log.warning("telegram_send_failed", chat_id=chat_id, error=type(e).__name__)
                        try:
                            await self._flood_safe(
                                lambda c=chat_id, m=m, k=markup: bot.send_message(
                                    c, m.text[:TEXT_LIMIT], reply_markup=k
                                )
                            )
                            delivered += 1
                        except Exception as e2:
                            log.error(
                                "telegram_send_failed_again",
                                chat_id=chat_id,
                                error=type(e2).__name__,
                            )
        finally:
            await bot.session.close()
        return delivered
