"""Notifier: доставка сообщений админам. Telegram — реальная реализация, Fake — для тестов."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol

from radar.log import get_logger
from radar.schemas import OutMessage

log = get_logger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


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


class TelegramNotifier:
    def __init__(self, token: str, admin_ids: Sequence[int]) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан")
        if not admin_ids:
            raise ValueError("ADMIN_IDS пуст: некому отправлять")
        self._token = token
        self.admin_ids = list(admin_ids)

    def send(self, messages: Sequence[OutMessage]) -> int:
        return asyncio.run(self._send_all(messages))

    async def _send_all(self, messages: Sequence[OutMessage]) -> int:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from radar.bot.keyboards import to_markup

        delivered = 0
        bot = Bot(self._token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            for chat_id in self.admin_ids:
                for m in messages:
                    markup = to_markup(m.buttons) if m.buttons else None
                    try:
                        if m.photo_url and len(m.text) <= CAPTION_LIMIT:
                            await bot.send_photo(
                                chat_id, m.photo_url, caption=m.text, reply_markup=markup
                            )
                        else:
                            await bot.send_message(
                                chat_id,
                                m.text[:TEXT_LIMIT],
                                reply_markup=markup,
                                disable_web_page_preview=False,
                            )
                        delivered += 1
                    except Exception as e:  # одна битая карточка не валит весь дайджест
                        log.warning("telegram_send_failed", chat_id=chat_id, error=type(e).__name__)
                        try:
                            await bot.send_message(
                                chat_id, m.text[:TEXT_LIMIT], reply_markup=markup
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
