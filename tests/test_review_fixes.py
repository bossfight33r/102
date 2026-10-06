"""Регрессии по итогам ревью: недоставленные сообщения, удалённые каналы, атомарные миграции,
длинные сообщения трендов, учёт стоимости пустых ответов LLM."""

import re
import sqlite3
from datetime import timedelta

import pytest

import radar.db as dbmod
from radar.analyze.analyzer import Analyzer
from radar.collect.watchlist import poll_watchlist, stale_subs
from radar.delivery import MAX_SEND_ATTEMPTS
from radar.digest.build import send_digest
from radar.llm.base import LLMError
from radar.llm.fake import FakeLLM
from radar.schemas import ChannelStatus, LLMResponse, NicheTrends, TrendFeature, TrendReport
from radar.score.outliers import score_all
from radar.tick import run_tick
from radar.trends import render_trends_text


class DeadNotifier:
    def __init__(self):
        self.calls = 0

    def send(self, messages):
        self.calls += 1
        return 0


def test_undelivered_digest_retried_then_given_up(app, now):
    dead = DeadNotifier()
    for i in range(MAX_SEND_ATTEMPTS - 1):
        r = send_digest(app.db, app.config, dead, now + timedelta(minutes=15 * i))
        assert r.deferred
    r = send_digest(app.db, app.config, dead, now + timedelta(hours=2))
    assert not r.deferred  # сдались — помечен, чтобы не долбить Telegram бесконечно
    assert (
        send_digest(app.db, app.config, dead, now + timedelta(hours=3)).message == "уже отправлен"
    )
    assert dead.calls == MAX_SEND_ATTEMPTS


def test_undelivered_alerts_stay_queued(app, fake_yt, now):
    app._notifier = DeadNotifier()
    fake_yt.quota_exceeded = True
    run_tick(app, now)
    assert len(app.db.pending_alerts()) == 1


def test_deleted_channel_not_refreshed_forever(app, fake_yt, now):
    ch = app.youtube.resolve_channel(
        "@techguru", purpose="t", now=now, status=ChannelStatus.WATCHING
    )
    app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    del fake_yt.channels[ch.id]  # канал удалён
    later = now + timedelta(days=2)
    poll_watchlist(app.youtube, app.db, app.config, later)
    assert stale_subs(app.db, app.config, later + timedelta(minutes=15)) == []


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    dbmod.Database(path).close()
    bad = dbmod.SCHEMA_VERSION + 1
    monkeypatch.setattr(
        dbmod, "_MIGRATIONS", [*dbmod._MIGRATIONS, (bad, "CREATE TABLE x(a); SELECT nope();")]
    )
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", bad)
    with pytest.raises(sqlite3.Error):
        dbmod.Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == bad - 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='x'").fetchone() is None


def test_long_trends_text_is_valid_html(now):
    feats = [
        TrendFeature(
            category="topic",
            name=f"слово{i}&<x>",
            n_outliers=3,
            n_all=5,
            outlier_share=0.5,
            base_share=0.1,
            lift=3.0,
        )
        for i in range(40)
    ]
    niches = [
        NicheTrends(
            niche_id=f"n{i}",
            niche_name=f"Ниша {i}",
            days=7,
            n_videos=50,
            n_outliers=6,
            features=feats,
        )
        for i in range(30)
    ]
    text = render_trends_text(TrendReport(generated_at=now, days=7, niches=niches))
    assert len(text) <= 4096
    assert text.count("<b>") == text.count("</b>")
    assert not re.search(r"&[a-z]*$", text) and "<x>" not in text


def test_empty_max_tokens_response_cost_recorded(app, now):
    ch = app.youtube.resolve_channel(
        "@techguru", purpose="t", now=now, status=ChannelStatus.WATCHING
    )
    app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)

    class Burner(FakeLLM):
        def complete(self, **kw):
            self.calls.append(kw)
            raise LLMError(
                "max_tokens",
                response=LLMResponse(text="", model="m", output_tokens=16000, cost=0.32),
            )

    with pytest.raises(LLMError):
        Analyzer(app.db, app.youtube, Burner(), app.config, app.profile).analyze("tgLONGOUT01", now)
    assert app.db.llm_cost_since(now - timedelta(hours=1)) == pytest.approx(0.32)
