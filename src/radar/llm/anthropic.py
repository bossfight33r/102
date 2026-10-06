"""LLMProvider на официальном Anthropic SDK. Модель — из env (ANTHROPIC_MODEL)."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import anthropic

from radar.config import LLMConfig
from radar.llm.base import LLMError
from radar.log import get_logger
from radar.schemas import BatchItemResult, ImageInput, LLMRequest, LLMResponse

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

    def _params(
        self,
        system: str,
        prompt: str,
        images: Sequence[ImageInput],
        max_tokens: int,
        effort: str | None,
    ) -> dict[str, Any]:
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
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if effort or self.cfg.effort:
            params["output_config"] = {"effort": effort or self.cfg.effort}
        return params

    def _parse(self, resp: Any, max_tokens: int, discount: float = 1.0) -> LLMResponse:
        """Message → LLMResponse. Отказ и пустой ответ при max_tokens → LLMError."""
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
            cost=self.cost(in_tok, out_tok) * discount,
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

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 4000,
        effort: str | None = None,
    ) -> LLMResponse:
        kwargs = self._params(system, prompt, images, max_tokens, effort)
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
        return self._parse(resp, max_tokens)

    # --- Batch API (−50%; fallbacks в пакетах не поддерживаются) -------------------------

    def _batch_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except anthropic.APIConnectionError as e:
            raise LLMError(f"нет связи с Anthropic API: {type(e).__name__}") from None
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic Batch API: HTTP {e.status_code}") from None

    def submit_batch(self, requests: Sequence[LLMRequest]) -> str:
        batch = self._batch_call(
            self._client.messages.batches.create,
            requests=[
                {
                    "custom_id": r.custom_id,
                    "params": self._params(r.system, r.prompt, r.images, r.max_tokens, r.effort),
                }
                for r in requests
            ],
        )
        return str(batch.id)

    def batch_ended(self, batch_id: str) -> bool:
        batch = self._batch_call(self._client.messages.batches.retrieve, batch_id)
        return batch.processing_status == "ended"

    def batch_results(self, batch_id: str) -> list[BatchItemResult]:
        out: list[BatchItemResult] = []
        for item in self._batch_call(self._client.messages.batches.results, batch_id):
            kind = item.result.type
            if kind != "succeeded":
                error = getattr(getattr(item.result, "error", None), "type", "") or kind
                out.append(BatchItemResult(custom_id=item.custom_id, error=f"batch: {error}"))
                continue
            try:
                resp = self._parse(
                    item.result.message, self.cfg.max_tokens, self.cfg.batch_discount
                )
                out.append(BatchItemResult(custom_id=item.custom_id, response=resp))
            except LLMError as e:
                out.append(
                    BatchItemResult(custom_id=item.custom_id, response=e.response, error=str(e))
                )
        return out
