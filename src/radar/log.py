"""structlog с обязательной маскировкой секретов."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.I)
_URL_KEY_RE = re.compile(r"([?&](?:key|token)=)[^&\s]+", re.I)
_TG_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
_ANTHROPIC_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{30,}")

_known_secrets: set[str] = set()

MASK = "***"


def register_secrets(values: list[str]) -> None:
    """Зарегистрировать конкретные значения секретов: они вырезаются из любых строк лога."""
    _known_secrets.update(v for v in values if v and len(v) >= 4)


def redact_text(text: str) -> str:
    for s in _known_secrets:
        if s in text:
            text = text.replace(s, MASK)
    text = _URL_KEY_RE.sub(r"\1" + MASK, text)
    text = _TG_TOKEN_RE.sub(MASK, text)
    text = _ANTHROPIC_RE.sub(MASK, text)
    return _GOOGLE_KEY_RE.sub(MASK, text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            k: (MASK if _SECRET_KEY_RE.search(str(k)) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    if isinstance(value, BaseException):
        return redact_text(repr(value))
    return value


def redact_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _redact_value(event_dict)


def _stderr_logger(*_: Any) -> structlog.PrintLogger:
    # sys.stderr берётся в момент записи: переживает подмену потоков (pytest, launchd).
    return structlog.PrintLogger(file=sys.stderr)


def configure_logging(
    level: str = "INFO", json: bool = False, secrets: list[str] | None = None
) -> None:
    register_secrets(secrets or [])
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=False,
    )
    # httpx логирует URL запроса (с ключом в query) на INFO — глушим.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
