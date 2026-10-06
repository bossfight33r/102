from types import SimpleNamespace

import pytest

from radar.config import LLMConfig
from radar.llm.anthropic import FALLBACK_BETA, AnthropicLLM
from radar.llm.base import LLMError, extract_json
from radar.llm.fake import ANALYSIS_MARKER, FakeLLM
from radar.schemas import ImageInput


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Вот:\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('prefix {"a": 3} suffix') == {"a": 3}
    with pytest.raises(LLMError):
        extract_json("нет json")


class FakeMessages:
    def __init__(self, resp):
        self.resp = resp
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


def fake_client(stop_reason="end_turn"):
    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="ok"),
        ],
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000),
    )
    msgs = FakeMessages(resp)
    return SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs)), msgs


def test_anthropic_request_shape_and_cost():
    client, msgs = fake_client()
    llm = AnthropicLLM("k", "claude-opus-5-5", LLMConfig(), client=client)
    r = llm.complete(system="s", prompt="p", images=[ImageInput(data=b"\xff\xd8jpg")])
    content = msgs.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["type"] == "base64"
    assert content[-1] == {"type": "text", "text": "p"}
    assert msgs.kwargs["betas"] == [FALLBACK_BETA] and msgs.kwargs["fallbacks"] == "default"
    assert msgs.kwargs["output_config"] == {"effort": "medium"}
    assert r.text == "ok"
    assert r.cost == pytest.approx(4.0 + 2.0)


def test_anthropic_without_fallback():
    client, msgs = fake_client()
    AnthropicLLM("k", "m", LLMConfig(refusal_fallback=False, effort=None), client=client).complete(
        system="s", prompt="p"
    )
    assert "betas" not in msgs.kwargs and "output_config" not in msgs.kwargs


def test_anthropic_refusal_raises():
    client, _ = fake_client("refusal")
    with pytest.raises(LLMError):
        AnthropicLLM("k", "m", LLMConfig(), client=client).complete(system="s", prompt="p")


def test_fake_llm_analysis():
    llm = FakeLLM()
    r = llm.complete(system=f"... {ANALYSIS_MARKER}", prompt="x")
    assert "why_it_worked" in extract_json(r.text)
    assert len(llm.calls) == 1
