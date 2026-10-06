"""Протокол LLM-провайдера (текст + изображения)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Protocol

from radar.schemas import BatchItemResult, ImageInput, LLMRequest, LLMResponse


class LLMError(Exception):
    """Ошибка провайдера: сеть, отказ модели, невалидный ответ.

    response — оплаченный ответ без полезного текста (например, max_tokens ушёл на мышление):
    вызывающий код учитывает его стоимость в llm_usage.
    """

    def __init__(self, message: str, response: LLMResponse | None = None) -> None:
        super().__init__(message)
        self.response = response


class LLMProvider(Protocol):
    model: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 4000,
        effort: str | None = None,
    ) -> LLMResponse:
        """effort переопределяет llm.effort из конфига. Мышление модели расходует max_tokens."""
        ...


class BatchLLMProvider(LLMProvider, Protocol):
    """Провайдер с пакетной обработкой (Batch API: −50% стоимости, ответ до 24 ч)."""

    def submit_batch(self, requests: Sequence[LLMRequest]) -> str: ...

    def batch_ended(self, batch_id: str) -> bool: ...

    def batch_results(self, batch_id: str) -> list[BatchItemResult]: ...


def supports_batch(llm: object) -> bool:
    return all(hasattr(llm, m) for m in ("submit_batch", "batch_ended", "batch_results"))


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
