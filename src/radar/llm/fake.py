"""FakeLLM для тестов и демо-режима: детерминированные ответы, считает вызовы."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from radar.schemas import ImageInput, LLMResponse

ANALYSIS_MARKER = "ANALYSIS_JSON"

DEFAULT_ANALYSIS = {
    "why_it_worked": {
        "title_pattern": "Число + конкретная выгода + интрига",
        "thumbnail_elements": ["крупное лицо с эмоцией", "3 слова текста", "контрастный фон"],
        "topic": "практичный инструмент, который экономит время",
        "format": "пошаговый разбор на реальном примере",
        "duration": "12–15 минут, плотный темп без воды",
    },
    "hook_formula": "Показать результат в первые 5 секунд, затем обещать путь к нему",
    "audience_questions": ["Работает ли это бесплатно?", "Как настроить на Mac?"],
    "idea_for_my_channel": {
        "title": "Я заменил 3 рутинные задачи одной связкой — показываю на своём проекте",
        "pitch": "Тот же паттерн «результат сразу», но на моём реальном кейсе и для моей аудитории",
        "difference_from_original": "Свой кейс и свои данные вместо обзора чужого инструмента",
        "key_points": ["Результат до/после", "Пошаговая настройка", "Ошибки, которые я сделал"],
    },
    "short_form_angle": "30 секунд: результат → 3 шага → призыв смотреть полное видео",
    "confidence": 0.7,
}


class FakeLLM:
    def __init__(
        self,
        model: str = "fake-llm",
        responder: Callable[[str, str], str] | None = None,
        cost: float = 0.001,
    ) -> None:
        self.model = model
        self.responder = responder
        self.cost = cost
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 2000,
    ) -> LLMResponse:
        self.calls.append({"system": system, "prompt": prompt, "images": len(images)})
        if self.responder:
            text = self.responder(system, prompt)
        elif ANALYSIS_MARKER in system:
            text = "```json\n" + json.dumps(DEFAULT_ANALYSIS, ensure_ascii=False) + "\n```"
        else:
            text = "Fake summary: темы с числом в заголовке растут."
        return LLMResponse(
            text=text,
            model=self.model,
            input_tokens=len(system + prompt) // 4,
            output_tokens=len(text) // 4,
            cost=self.cost,
        )
