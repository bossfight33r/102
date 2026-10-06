"""Ограничение повторных отправок: недоставленное сообщение повторяется, но не бесконечно."""

from __future__ import annotations

from radar.db import Database
from radar.log import get_logger

log = get_logger(__name__)

MAX_SEND_ATTEMPTS = 4


def give_up_delivery(db: Database, key: str, max_attempts: int = MAX_SEND_ATTEMPTS) -> bool:
    """Учесть неудачную попытку доставки. True — попыток больше нет, считаем доставку завершённой.

    Telegram может отвергать сообщение всегда (например, ошибка разметки) — без лимита tick
    пытался бы каждые 15 минут, а дайджест с LLM-вступлением ещё и тратил бы деньги.
    """
    k = f"send_attempts:{key}"
    attempts = int(db.get_kv(k) or 0) + 1
    db.set_kv(k, str(attempts))
    if attempts >= max_attempts:
        log.error("delivery_gave_up", key=key, attempts=attempts)
        return True
    return False
