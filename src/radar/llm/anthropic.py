"""LLMProvider на официальном Anthropic SDK. Модель — из env (ANTHROPIC_MODEL)."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import anthropic

from radar.config import LLMConfig
from radar.llm.base import LLMError
from radar.log import get_logger
from radar.schemas import ImageInput, LLMResponse

log = get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    def __init__(self, api_key: str, model: str, cfg: LLMConfig, client: Any | None = None) -> None:
        if not api_key and client is None:
            raise ValueError("ANTHROPIC_API_KEY не задан")
        self.model = model
        self.cfg = cfg
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120.0)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.cfg.input_usd_per_mtok
            + output_tokens * self.cfg.output_usd_per_mtok
        ) / 1_000_000

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 4000,
        effort: str | None = None,
    ) -> LLMResponse:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": base64.standard_b64encode(img.data).decode("ascii"),
                },
            }
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if effort or self.cfg.effort:
            kwargs["output_config"] = {"effort": effort or self.cfg.effort}
        try:
            if self.cfg.refusal_fallback:
                resp = self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                resp = self._client.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:
            raise LLMError(f"нет связи с Anthropic API: {type(e).__name__}") from None
        except anthropic.RateLimitError:
            raise LLMError("Anthropic API: rate limit") from None
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API: HTTP {e.status_code}") from None

        if resp.stop_reason == "refusal":
            raise LLMError("модель отказалась отвечать (refusal)")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = resp.usage
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        result = LLMResponse(
            text=text,
            model=str(resp.model),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost=self.cost(in_tok, out_tok),
        )
        if resp.stop_reason == "max_tokens":
            log.warning("llm_max_tokens", model=resp.model, output_tokens=out_tok)
            if not text.strip():
                raise LLMError(
                    f"лимит max_tokens={max_tokens} исчерпан до ответа (мышление модели) — "
                    "увеличьте llm.max_tokens / llm.light_max_tokens",
                    response=result,
                )
        return result
