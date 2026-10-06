"""FakeLLM для тестов и демо-режима: детерминированные ответы, считает вызовы."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from radar.schemas import BatchItemResult, ImageInput, LLMRequest, LLMResponse

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
        max_tokens: int = 4000,
        effort: str | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "images": len(images),
                "effort": effort,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
            }
        )
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


class FakeBatchLLM(FakeLLM):
    """FakeLLM с Batch API: пакет «готов» после ready_after проверок статуса."""

    def __init__(self, *args: object, ready_after: int = 0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.ready_after = ready_after
        self.batches: dict[str, list[LLMRequest]] = {}
        self.checks: dict[str, int] = {}
        self.fail_ids: set[str] = set()
        self.cancelled: set[str] = set()

    def submit_batch(self, requests: Sequence[LLMRequest]) -> str:
        batch_id = f"msgbatch_{len(self.batches) + 1}"
        self.batches[batch_id] = list(requests)
        self.checks[batch_id] = 0
        return batch_id

    def batch_ended(self, batch_id: str) -> bool:
        self.checks[batch_id] += 1
        return self.checks[batch_id] > self.ready_after

    def cancel_batch(self, batch_id: str) -> None:
        self.cancelled.add(batch_id)

    def batch_results(self, batch_id: str) -> list[BatchItemResult]:
        out = []
        for r in self.batches[batch_id]:
            if r.custom_id in self.fail_ids:
                out.append(BatchItemResult(custom_id=r.custom_id, error="batch: errored"))
                continue
            resp = self.complete(
                system=r.system, prompt=r.prompt, images=r.images, max_tokens=r.max_tokens
            )
            out.append(
                BatchItemResult(
                    custom_id=r.custom_id, response=resp.model_copy(update={"cost": resp.cost / 2})
                )
            )
        return out
