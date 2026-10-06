"""Универсальный OpenAI-совместимый провайдер: Gemini, DeepSeek, OpenAI, OpenRouter, Ollama."""

import json

import httpx
import pytest

from radar.app import App, ConfigError
from radar.config import LLMConfig, Settings
from radar.llm.base import LLMError
from radar.llm.openai_compat import PRESETS, OpenAICompatLLM, model_prices
from radar.log import configure_logging
from radar.schemas import ImageInput

KEY = "sk-test-0123456789abcdef0123456789"


def make(handler, provider="gemini", model="", base_url="", cfg=None, key=KEY, **kw):
    sleeps = []
    llm = OpenAICompatLLM(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=key,
        cfg=cfg or LLMConfig(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        **kw,
    )
    return llm, sleeps


def ok(text='{"a": 1}', finish="stop", model="gemini-3.5-flash-lite", usage=(1_000_000, 100_000)):
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": finish}
            ],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
        },
    )


def test_request_shape_gemini_with_image():
    seen = []

    def handler(req):
        seen.append(req)
        return ok()

    llm, _ = make(handler)
    r = llm.complete(
        system="sys", prompt="p", images=[ImageInput(data=b"\xff\xd8x")], json_mode=True
    )
    req = seen[0]
    assert (
        str(req.url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == "gemini-3.5-flash-lite" and body["max_tokens"] == 4000
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "p"}
    assert (
        parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        and "detail" not in parts[1]["image_url"]
    )
    assert body["response_format"] == {"type": "json_object"} and "reasoning_effort" not in body
    assert r.text == '{"a": 1}' and r.cost == pytest.approx(0.30 + 0.25)


def test_openai_preset_uses_max_completion_tokens_and_options():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return ok(model="gpt-5-mini")

    cfg = LLMConfig(image_detail="low", reasoning_effort="low", json_mode=False)
    llm, _ = make(handler, provider="openai", cfg=cfg)
    llm.complete(
        system="s", prompt="p", images=[ImageInput(data=b"x")], max_tokens=500, json_mode=True
    )
    body = seen[0]
    assert (
        body["model"] == "gpt-5-mini"
        and body["max_completion_tokens"] == 500
        and "max_tokens" not in body
    )
    assert body["messages"][1]["content"][1]["image_url"]["detail"] == "low"
    assert body["reasoning_effort"] == "low" and "response_format" not in body


def test_ollama_needs_no_key_and_is_free():
    seen = []

    def handler(req):
        seen.append(req)
        return ok(model="qwen3-vl:8b")

    llm, _ = make(handler, provider="ollama", key="")
    r = llm.complete(system="s", prompt="p")
    assert str(seen[0].url) == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in seen[0].headers and r.cost == 0


def test_custom_requires_url_and_model():
    with pytest.raises(ValueError):
        make(lambda r: ok(), provider="custom")
    llm, _ = make(
        lambda r: ok(), provider="custom", model="my-model", base_url="https://llm.example.com/v1/"
    )
    assert (
        llm.base_url == "https://llm.example.com/v1"
        and llm.prices() is None
        and llm.cost(10, 10) == 0
    )


def test_missing_key_rejected():
    with pytest.raises(ValueError):
        make(lambda r: ok(), provider="deepseek", key="")


def test_retry_then_success_and_errors():
    calls = []

    def handler(req):
        calls.append(1)
        return (
            httpx.Response(429, json={"error": {"message": "slow down"}})
            if len(calls) < 3
            else ok()
        )

    llm, sleeps = make(handler)
    assert llm.complete(system="s", prompt="p").text
    assert len(sleeps) == 2

    llm, _ = make(lambda r: httpx.Response(400, json=[{"error": {"message": "bad model"}}]))
    with pytest.raises(LLMError, match="HTTP 400 bad model"):
        llm.complete(system="s", prompt="p")

    def boom(req):
        raise httpx.ConnectError("down")

    llm, _ = make(boom)
    with pytest.raises(LLMError, match="нет связи"):
        llm.complete(system="s", prompt="p")


def test_length_and_content_filter():
    llm, _ = make(lambda r: ok(text="", finish="length"))
    with pytest.raises(LLMError) as e:
        llm.complete(system="s", prompt="p")
    assert e.value.response is not None and e.value.response.cost > 0  # оплачено — учтём
    llm, _ = make(lambda r: ok(text="x", finish="content_filter"))
    with pytest.raises(LLMError, match="фильтром"):
        llm.complete(system="s", prompt="p")


def test_content_as_parts_list():
    llm, _ = make(lambda r: ok(text=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))
    assert llm.complete(system="s", prompt="p").text == "ab"


def test_prices_table_and_override():
    assert model_prices("openrouter", "google/gemini-3.5-flash-lite") == (0.30, 2.50)
    assert model_prices("ollama", "anything") == (0.0, 0.0)
    assert model_prices("gemini", "unknown-model") is None
    llm, _ = make(lambda r: ok(), cfg=LLMConfig(input_usd_per_mtok=1.0, output_usd_per_mtok=2.0))
    assert llm.prices() == (1.0, 2.0)


def test_key_not_in_logs(capsys):
    configure_logging("DEBUG", json=True, secrets=[KEY])
    llm, _ = make(
        lambda r: httpx.Response(500, json={"error": {"message": f"echo {KEY}"}}), max_retries=0
    )
    with pytest.raises(LLMError) as e:
        llm.complete(system="s", prompt="p")
    out = capsys.readouterr()
    assert KEY not in out.err + out.out
    import structlog

    structlog.get_logger().error("x", err=str(e.value))
    assert KEY not in capsys.readouterr().err


@pytest.mark.parametrize("provider", ["gemini", "deepseek", "openai", "openrouter", "ollama"])
def test_app_builds_provider(settings, db, provider):
    s = settings.model_copy(
        update={
            "llm_provider": provider,
            "llm_api_key": None if provider == "ollama" else __import__("pydantic").SecretStr("k"),
        }
    )
    app = App.build(s, db=db)
    assert app.has_llm()
    assert isinstance(app.llm, OpenAICompatLLM) and app.llm.model == PRESETS[provider].model


def test_app_provider_errors(settings, db):
    app = App.build(
        settings.model_copy(
            update={"llm_provider": "nope", "llm_api_key": __import__("pydantic").SecretStr("k")}
        ),
        db=db,
    )
    with pytest.raises(ConfigError):
        _ = app.llm
    app = App.build(
        settings.model_copy(update={"llm_provider": "gemini", "llm_api_key": None}), db=db
    )
    assert not app.has_llm()
    app = App.build(
        settings.model_copy(
            update={
                "llm_provider": "anthropic",
                "anthropic_api_key": __import__("pydantic").SecretStr("k"),
            }
        ),
        db=db,
    )
    from radar.llm.anthropic import AnthropicLLM

    assert isinstance(app.llm, AnthropicLLM) and app.llm.model == "claude-haiku-4-5"


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    s = Settings(_env_file=None)
    assert s.llm_provider == "deepseek" and "secret" in s.secret_values()


def test_analysis_uses_json_mode(app, fake_llm, now):
    from radar.analyze.analyzer import Analyzer
    from radar.collect.adhoc import ensure_video

    ensure_video(app.youtube, app.db, "tgLONGOUT01", now)
    Analyzer(app.db, app.youtube, fake_llm, app.config, app.profile).analyze("tgLONGOUT01", now)
    assert fake_llm.calls[0]["json_mode"] is True


def test_tick_end_to_end_with_openai_compat(settings, db, fake_yt, notifier, now):
    """Весь конвейер с настоящим OpenAICompatLLM (MockTransport отвечает JSON-анализом)."""
    from radar.llm.fake import DEFAULT_ANALYSIS
    from radar.tick import run_tick

    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return ok(
            text="```json\n" + json.dumps(DEFAULT_ANALYSIS, ensure_ascii=False) + "\n```",
            usage=(5000, 1200),
        )

    llm, _ = make(handler, provider="deepseek")
    app = App.build(settings, db=db, youtube_client=fake_yt, llm=llm, notifier=notifier)
    results = {r.name: r for r in run_tick(app, now)}
    assert results["analyze"].stats["done"] == 2
    a = db.get_analysis("tgLONGOUT01")
    assert a.model == "gemini-3.5-flash-lite"  # имя модели — из ответа провайдера
    assert a.cost == pytest.approx((5000 * 0.14 + 1200 * 0.28) / 1e6)
    assert all(b["response_format"] == {"type": "json_object"} for b in bodies)
    assert any("Идея" in m.text for m in notifier.sent)
