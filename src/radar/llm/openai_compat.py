"""LLMProvider для любого OpenAI-совместимого API (Chat Completions): Gemini, DeepSeek, OpenAI,
OpenRouter, локальная Ollama или свой адрес. Прямые REST-запросы через httpx, без отдельного SDK."""

from __future__ import annotations

import base64
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from radar.config import LLMConfig
from radar.llm.base import LLMError
from radar.log import get_logger
from radar.schemas import ImageInput, LLMResponse

log = get_logger(__name__)


@dataclass(frozen=True)
class Preset:
    base_url: str
    model: str
    needs_key: bool = True
    # Reasoning-модели OpenAI принимают только max_completion_tokens.
    max_tokens_param: str = "max_tokens"


PRESETS: dict[str, Preset] = {
    "gemini": Preset(
        "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash-lite"
    ),
    "deepseek": Preset("https://api.deepseek.com/v1", "deepseek-flash"),
    "openai": Preset(
        "https://api.openai.com/v1", "gpt-5-mini", max_tokens_param="max_completion_tokens"
    ),
    "openrouter": Preset("https://openrouter.ai/api/v1", "google/gemini-3.5-flash-lite"),
    "ollama": Preset("http://localhost:11434/v1", "qwen3-vl:8b", needs_key=False),
    "custom": Preset("", ""),
}

# Ориентировочные цены $/1M токенов (вход, выход) по агрегаторам на октябрь 2026 — сверяйте
# с сайтом провайдера; точные цены можно задать в settings.yaml (llm.input/output_usd_per_mtok).
PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "deepseek-flash": (0.14, 0.28),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}
LOCAL_PROVIDERS = {"ollama"}

_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


def model_prices(provider: str, model: str) -> tuple[float, float] | None:
    """Цены модели или None, если неизвестны. Префикс OpenRouter («google/…») отбрасывается."""
    if provider in LOCAL_PROVIDERS:
        return (0.0, 0.0)
    return PRICES.get(model.split("/", 1)[-1])


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        cfg: LLMConfig,
        http: httpx.Client | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        preset = PRESETS.get(provider, PRESETS["custom"])
        self.provider = provider
        self.model = model or preset.model
        self.base_url = (base_url or preset.base_url).rstrip("/")
        if not self.base_url or not self.model:
            raise ValueError(f"для LLM_PROVIDER={provider} задайте LLM_BASE_URL и LLM_MODEL")
        if preset.needs_key and not api_key:
            raise ValueError(f"LLM_API_KEY не задан (провайдер {provider})")
        self._key = api_key
        self._max_tokens_param = preset.max_tokens_param
        self.cfg = cfg
        self._http = http or httpx.Client(timeout=180.0)
        self._max_retries = max_retries
        self._sleep = sleep

    def prices(self) -> tuple[float, float] | None:
        if self.cfg.input_usd_per_mtok is not None and self.cfg.output_usd_per_mtok is not None:
            return (self.cfg.input_usd_per_mtok, self.cfg.output_usd_per_mtok)
        return model_prices(self.provider, self.model)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        p = self.prices()
        if p is None:
            return 0.0  # неизвестная цена — doctor предупреждает, дневной лимит не работает
        return (input_tokens * p[0] + output_tokens * p[1]) / 1_000_000

    def _body(
        self,
        system: str,
        prompt: str,
        images: Sequence[ImageInput],
        max_tokens: int,
        json_mode: bool,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            url = f"data:{img.media_type};base64,{base64.standard_b64encode(img.data).decode('ascii')}"
            image: dict[str, Any] = {"url": url}
            if self.cfg.image_detail:
                image["detail"] = self.cfg.image_detail
            content.append({"type": "image_url", "image_url": image})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            self._max_tokens_param: max_tokens,
        }
        if json_mode and self.cfg.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.cfg.reasoning_effort:
            body["reasoning_effort"] = self.cfg.reasoning_effort
        return body

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        url = f"{self.base_url}/chat/completions"
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http.post(url, json=body, headers=headers)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise LLMError(f"нет связи с {self.provider}: {type(e).__name__}") from None
                self._backoff(attempt, "network")
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                self._backoff(attempt, str(resp.status_code))
                continue
            raise LLMError(f"{self.provider}: HTTP {resp.status_code} {_error_message(resp)}")
        raise LLMError(f"{self.provider}: попытки исчерпаны")  # pragma: no cover

    def _backoff(self, attempt: int, why: str) -> None:
        delay = 2**attempt + random.uniform(0, 0.5)
        log.info(
            "llm_retry", provider=self.provider, attempt=attempt + 1, delay=round(delay, 2), why=why
        )
        self._sleep(delay)

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
        data = self._post(self._body(system, prompt, images, max_tokens, json_mode))
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.provider}: пустой ответ без choices")
        choice = choices[0]
        text = (choice.get("message") or {}).get("content") or ""
        if isinstance(text, list):  # некоторые провайдеры отдают части контента
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        result = LLMResponse(
            text=text,
            model=str(data.get("model") or self.model),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost=self.cost(in_tok, out_tok),
        )
        finish = choice.get("finish_reason")
        if finish == "content_filter":
            raise LLMError(f"{self.provider}: ответ заблокирован фильтром", response=result)
        if finish == "length" and not text.strip():
            raise LLMError(
                f"лимит max_tokens={max_tokens} исчерпан до ответа — увеличьте llm.max_tokens",
                response=result,
            )
        return result


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, list) and data:  # Gemini иногда отдаёт список ошибок
        data = data[0]
    err = data.get("error", data) if isinstance(data, dict) else {}
    msg = err.get("message", "") if isinstance(err, dict) else str(err)
    return str(msg)[:200]
