"""Протокол LLM-провайдера (текст + изображения)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Protocol

from radar.schemas import ImageInput, LLMResponse


class LLMError(Exception):
    """Ошибка провайдера: сеть, отказ модели, невалидный ответ."""


class LLMProvider(Protocol):
    model: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 2000,
    ) -> LLMResponse: ...


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """JSON из ответа модели: целиком, из ```json``` блока или от первой { до последней }."""
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE_RE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
    raise LLMError("в ответе модели нет валидного JSON")
