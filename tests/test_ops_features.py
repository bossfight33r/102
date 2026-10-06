"""Разбор любого ролика, /add, расход LLM, watchdog tick, radar topics."""

import asyncio
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from radar.bot.handlers import cmd_add_channel, cmd_analyze, cmd_quota, stale_tick_alert
from radar.bot.main import build_dispatcher
from radar.cli import app as cli
from radar.collect.adhoc import ensure_video, parse_video_ref
from radar.schemas import ChannelStatus
from test_bot import ADMIN, FakeSession, _message_update


@pytest.mark.parametrize(
    "ref",
    [
        "tgLONGOUT01",
        "https://www.youtube.com/watch?v=tgLONGOUT01",
        "https://youtube.com/watch?feature=share&v=tgLONGOUT01&t=10",
        "https://youtu.be/tgLONGOUT01?si=abc",
        "https://www.youtube.com/shorts/tgLONGOUT01",
        "https://m.youtube.com/live/tgLONGOUT01",
    ],
)
def test_parse_video_ref(ref):
    assert parse_video_ref(ref) == "tgLONGOUT01"


def test_parse_video_ref_rejects_garbage():
    assert parse_video_ref("https://example.com/x") is None
    assert parse_video_ref("short") is None


def test_ensure_video_fetches_once(app, fake_yt, now):
    v = ensure_video(app.youtube, app.db, "https://youtu.be/sbBREAKOUT1", now)
    assert v.id == "sbBREAKOUT1" and app.db.snapshots_for(v.id)[0].views == 45_000
    ensure_video(app.youtube, app.db, "sbBREAKOUT1", now)
    assert len(fake_yt.calls_of("videos.list")) == 1
    with pytest.raises(ValueError):
        ensure_video(app.youtube, app.db, "zzzzzzzzzzz", now)


def test_cmd_analyze_any_video(app, fake_llm, now):
    msgs = cmd_analyze(app, "https://youtu.be/tgLONGOUT01", now)
    assert "Идея для моего канала" in msgs[0].text and len(fake_llm.calls) == 1
    assert "Не получилось" in cmd_analyze(app, "мусор", now)[0].text
    assert "Использование" in cmd_analyze(app, "", now)[0].text


def test_cmd_add_channel(app, now):
    assert "✅ Tech Guru" in cmd_add_channel(app, "@techguru ai-tools", now)
    ch = app.db.get_channel("UCtechguru00000000000000")
    assert ch.status == ChannelStatus.WATCHING and ch.niche_ids == ["ai-tools"]
    assert "не найдена" in cmd_add_channel(app, "@techguru nope", now)
    assert "не найден" in cmd_add_channel(app, "@nobody", now)
    assert "Использование" in cmd_add_channel(app, "", now)


def test_quota_shows_llm_spend(app, now):
    app.db.add_llm_usage("analysis:x", "m", 1, 1, 0.25, now - timedelta(hours=2))
    app.db.add_llm_usage("analysis:y", "m", 1, 1, 0.5, now - timedelta(days=3))
    text = cmd_quota(app, now)
    assert "$0.25 за 24 ч" in text and "$0.75 за 7 дней" in text


def test_watchdog_alerts_once_per_stall(app, now):
    assert stale_tick_alert(app, now) is None  # tick ещё не было
    app.db.set_kv("last_tick_at", now.isoformat())
    assert stale_tick_alert(app, now + timedelta(minutes=30)) is None
    text = stale_tick_alert(app, now + timedelta(hours=2))
    assert text and "2.0 ч" in text
    assert stale_tick_alert(app, now + timedelta(hours=3)) is None  # уже предупредили
    app.db.set_kv("last_tick_at", (now + timedelta(hours=4)).isoformat())  # tick ожил и снова встал
    assert stale_tick_alert(app, now + timedelta(hours=6)) is not None
    app.config.bot.watchdog_minutes = 0
    app.db.set_kv("last_tick_at", now.isoformat())
    assert stale_tick_alert(app, now + timedelta(days=1)) is None


def test_bot_add_command_wiring(app, now, monkeypatch):
    monkeypatch.setenv("RADAR_NOW", now.isoformat())
    session = FakeSession()
    from aiogram import Bot

    bot = Bot("123456:" + "A" * 35, session=session)
    dp = build_dispatcher(app)
    asyncio.run(dp.feed_update(bot, _message_update(ADMIN, "/add @techguru")))
    assert app.db.get_channel("UCtechguru00000000000000") is not None
    assert len(session.requests) == 1


def test_cli_topics_and_analyze_url(cli_app):
    r = CliRunner().invoke(cli, ["topics"])
    assert "Тем пока нет" in r.output
    r = CliRunner().invoke(cli, ["analyze", "https://youtu.be/tgSHORTOUT1"])
    assert r.exit_code == 0, r.output
    from radar.bot.handlers import handle_feedback

    handle_feedback(cli_app, "t", "tgSHORTOUT1", cli_app.db.get_video("tgSHORTOUT1").published_at)
    r = CliRunner().invoke(cli, ["topics"])
    assert "1. " in r.output and "youtu.be/tgSHORTOUT1" in r.output
    r = CliRunner().invoke(cli, ["quota"])
    assert "LLM: $" in r.output
