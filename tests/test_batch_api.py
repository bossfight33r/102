"""Batch API: подача пачкой, сбор на следующих tick, стоимость −50%, ошибки, таймаут, ожидание дайджеста."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from radar.analyze.analyzer import Analyzer
from radar.collect.watchlist import poll_watchlist
from radar.config import LLMConfig
from radar.llm.anthropic import AnthropicLLM
from radar.llm.fake import DEFAULT_ANALYSIS, FakeBatchLLM
from radar.schemas import ChannelStatus, LLMRequest
from radar.score.outliers import score_all
from radar.tick import run_tick

OUTLIERS = {"tgLONGOUT01", "tgSHORTOUT1", "sbBREAKOUT1"}


@pytest.fixture
def scored(app, now):
    for ref in ("@techguru", "@smallbuilder"):
        ch = app.youtube.resolve_channel(
            ref, purpose="t", now=now, niche_ids=["ai-tools"], status=ChannelStatus.WATCHING
        )
        app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)
    app.config.analysis.use_batch = True
    return app


def analyzer(app, llm):
    return Analyzer(app.db, app.youtube, llm, app.config, app.profile)


def test_batch_submit_then_collect(scored, now):
    llm = FakeBatchLLM(cost=0.02, ready_after=1)
    an = analyzer(scored, llm)
    r = an.analyze_pending(now)
    assert r.stats["submitted"] == 3 and r.stats["in_flight"] == 3 and not llm.calls
    assert {q.custom_id for q in next(iter(llm.batches.values()))} == OUTLIERS
    assert an.pending(now) == [] and an.awaiting(now)

    r2 = an.analyze_pending(now + timedelta(minutes=15))  # ещё не готов
    assert r2.stats["collected"] == 0 and r2.stats["submitted"] == 0

    r3 = an.analyze_pending(now + timedelta(minutes=30))
    assert r3.stats["collected"] == 3
    a = scored.db.get_analysis("tgLONGOUT01")
    assert a.cost == pytest.approx(0.01)  # −50%
    assert not an.awaiting(now + timedelta(minutes=45))
    assert an.cached(scored.db.get_video("tgLONGOUT01")) is not None  # хеш входа сохранён верно


def test_batch_failures_backoff(scored, now):
    def responder(system, prompt):
        return (
            "мусор" if "sbBREAKOUT1" in prompt or "SaaS" in prompt else json.dumps(DEFAULT_ANALYSIS)
        )

    llm = FakeBatchLLM(responder=responder)
    llm.fail_ids.add("tgSHORTOUT1")
    an = analyzer(scored, llm)
    an.analyze_pending(now)
    r = an.analyze_pending(now + timedelta(minutes=15))
    assert r.stats["collected"] == 1 and r.stats["failed"] == 2
    assert an.pending(now + timedelta(hours=1)) == []  # бэкофф 24 ч
    assert set(an.pending(now + timedelta(hours=25))) == {"tgSHORTOUT1", "sbBREAKOUT1"}


def test_batch_timeout(scored, now):
    llm = FakeBatchLLM(ready_after=10**6)
    an = analyzer(scored, llm)
    an.analyze_pending(now)
    r = an.analyze_pending(now + timedelta(hours=26))
    assert r.stats["failed"] == 3 and scored.db.open_llm_batches() == []


def test_batch_budget(scored, now):
    scored.db.add_llm_usage("x", "m", 1, 1, 5.0, now)
    r = analyzer(scored, FakeBatchLLM()).analyze_pending(now)
    assert r.deferred and r.stats["submitted"] == 0


def test_open_batches_collected_after_disabling(scored, now):
    llm = FakeBatchLLM()
    an = analyzer(scored, llm)
    an.analyze_pending(now)
    scored.config.analysis.use_batch = False
    an.analyze_pending(now + timedelta(minutes=15))
    assert scored.db.open_llm_batches() == [] and scored.db.get_analysis("tgLONGOUT01")


def test_tick_digest_waits_for_batch(scored, notifier, now):
    scored._llm = FakeBatchLLM(ready_after=1)
    run_tick(scored, now)  # 09:00 MSK: пакет подан, дайджест ждёт
    run_tick(scored, now + timedelta(minutes=15))  # пакет ещё не готов
    assert not any("Дайджест" in m.text for m in notifier.sent)
    run_tick(scored, now + timedelta(minutes=30))  # пакет готов → анализ → дайджест с идеями
    digest = [m.text for m in notifier.sent if "Идея" in m.text]
    assert len(digest) == 3


def test_anthropic_batch_shapes():
    created = {}

    def create(**kw):
        created.update(kw)
        return SimpleNamespace(id="msgbatch_1")

    msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0),
    )
    results = [
        SimpleNamespace(custom_id="a", result=SimpleNamespace(type="succeeded", message=msg)),
        SimpleNamespace(
            custom_id="b",
            result=SimpleNamespace(type="errored", error=SimpleNamespace(type="api_error")),
        ),
        SimpleNamespace(custom_id="c", result=SimpleNamespace(type="expired")),
    ]
    batches = SimpleNamespace(
        create=create,
        retrieve=lambda bid: SimpleNamespace(processing_status="ended"),
        results=lambda bid: iter(results),
    )
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    llm = AnthropicLLM("k", "claude-opus-5-5", LLMConfig(), client=client)
    bid = llm.submit_batch([LLMRequest(custom_id="a", system="s", prompt="p", max_tokens=100)])
    assert bid == "msgbatch_1"
    params = created["requests"][0]["params"]
    assert params["model"] == "claude-opus-5-5" and "fallbacks" not in params
    assert params["output_config"] == {"effort": "medium"}
    assert llm.batch_ended(bid)
    out = {r.custom_id: r for r in llm.batch_results(bid)}
    assert out["a"].response.cost == pytest.approx(2.0)  # $4/MTok × 0.5
    assert out["b"].error == "batch: api_error" and out["c"].error == "batch: expired"
