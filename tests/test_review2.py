"""Регрессии второго ревью: бюджет пакетов в полёте, отмена зависших пакетов, ошибки потока
результатов, атомарный сбор пакета, CSV-формулы, прогноз квоты, watchdog, потоки и SQLite."""

import asyncio
import sys
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from radar.analyze.analyzer import Analyzer
from radar.bot.main import watchdog_once
from radar.collect.watchlist import poll_watchlist
from radar.config import LLMConfig
from radar.export.suggestions import _csv_safe
from radar.llm.anthropic import AnthropicLLM
from radar.llm.base import LLMError
from radar.llm.fake import FakeBatchLLM
from radar.schemas import ChannelStatus
from radar.score.outliers import score_all
from radar.youtube.quota import forecast_daily_units


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


def test_batch_budget_counts_in_flight(scored, now):
    scored.config.analysis.max_cost_usd_per_day = 0.12
    scored.config.analysis.batch_cost_estimate_usd = 0.05
    an = Analyzer(
        scored.db, scored.youtube, FakeBatchLLM(ready_after=10), scored.config, scored.profile
    )
    r = an.analyze_pending(now)
    assert r.stats["submitted"] == 2  # 0.12 // 0.05
    r2 = an.analyze_pending(now + timedelta(minutes=15))
    assert r2.deferred and r2.stats["submitted"] == 0  # 2 в полёте × 0.05 — места нет


def test_expired_batch_cancelled(scored, now):
    llm = FakeBatchLLM(ready_after=10**6)
    an = Analyzer(scored.db, scored.youtube, llm, scored.config, scored.profile)
    an.analyze_pending(now)
    an.analyze_pending(now + timedelta(hours=26))
    assert llm.cancelled == {"msgbatch_1"}


def test_results_stream_error_is_llm_error():
    def results(bid):
        yield SimpleNamespace(custom_id="a")
        raise ConnectionResetError("stream dropped")

    client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(results=results)))
    with pytest.raises(LLMError):
        AnthropicLLM("k", "m", LLMConfig(), client=client).batch_results("b")


def test_collect_batch_atomic(scored, now, monkeypatch):
    llm = FakeBatchLLM(cost=0.02)
    an = Analyzer(scored.db, scored.youtube, llm, scored.config, scored.profile)
    an.analyze_pending(now)
    calls = {"n": 0}
    orig = an._save

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("db hiccup")
        return orig(*a, **kw)

    monkeypatch.setattr(an, "_save", flaky)
    with pytest.raises(RuntimeError):
        an.collect_batches(now + timedelta(minutes=15))
    assert scored.db.llm_cost_since(now - timedelta(days=1)) == 0  # откат: стоимость не записана
    assert len(scored.db.open_llm_batches()) == 1
    monkeypatch.setattr(an, "_save", orig)
    an.collect_batches(now + timedelta(minutes=30))
    assert scored.db.llm_cost_since(now - timedelta(days=1)) == pytest.approx(
        3 * 0.01
    )  # ровно один раз


@pytest.mark.parametrize("bad", ['=HYPERLINK("x")', "+1", "-cmd", "@SUM(A1)", "\tx"])
def test_csv_formula_escaped(bad):
    assert _csv_safe(bad) == "'" + bad
    assert _csv_safe("обычный текст") == "обычный текст" and _csv_safe(5) == 5


def test_forecast_long_intervals(scored, now):
    scored.config.snapshots.mid_interval_hours = 48
    scored.config.watchlist.channel_refresh_hours = 48
    rows = {name: u for name, u, _ in forecast_daily_units(scored.db, scored.config, now)}
    assert rows["подписчики"] >= 1 and rows[f"снимки ≤{scored.config.snapshots.mid_days}д"] >= 1


def test_watchdog_one_admin_failing(app, now):
    app.settings.admin_ids = [1, 2]
    app.db.set_kv("last_tick_at", now.isoformat())

    class Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text):
            if chat_id == 1:
                raise RuntimeError("blocked")
            self.sent.append(chat_id)

    bot = Bot()
    assert asyncio.run(watchdog_once(bot, app, now + timedelta(hours=3))) == 1
    assert bot.sent == [2]


def test_db_threads_do_not_interleave_transactions(db):
    db.set_kv("x", "0")
    errors = []
    start = threading.Barrier(2)

    def failing_tx():
        start.wait()
        try:
            with db.tx():
                db.set_kv("rolled", "yes")
                threading.Event().wait(0.05)
                raise RuntimeError("rollback me")
        except RuntimeError:
            pass

    def writer():
        start.wait()
        try:
            for i in range(20):
                db.set_kv(f"k{i}", "v")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=failing_tx), threading.Thread(target=writer)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert db.get_kv("rolled") is None  # откат только своей транзакции
    assert all(db.get_kv(f"k{i}") == "v" for i in range(20))  # чужие записи не потеряны


def test_cli_does_not_import_aiogram():
    code = "import sys, radar.cli, radar.digest.render; print('aiogram' in sys.modules)"
    import subprocess

    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
