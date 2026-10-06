"""radar me / /me и защита от повторяющихся идей."""

import json

import pytest
from typer.testing import CliRunner

from radar.analyze.analyzer import Analyzer, input_hash
from radar.bot.handlers import cmd_me, handle_feedback
from radar.cli import app as cli
from radar.collect.mine import my_channel_report, render_my_report
from radar.collect.watchlist import poll_watchlist
from radar.schemas import ChannelStatus
from radar.score.outliers import score_all


def test_my_channel_report(app, fake_yt, now):
    profile = app.profile.model_copy(update={"channel": "@techguru"})
    r = my_channel_report(app.youtube, profile, app.config, now)
    assert r.channel_title == "Tech Guru" and {b.format.value for b in r.baselines} == {
        "long",
        "short",
    }
    hits = {v.video_id for v in r.videos if v.is_outlier}
    assert hits == {"tgLONGOUT01", "tgSHORTOUT1"}
    assert r.videos[0].video_id in hits  # сортировка по score
    assert len(fake_yt.calls) == 3  # channels + playlistItems + videos
    text = render_my_report(r)
    assert "Мои аутлайеры</b>: 2" in text and "🔥" in text
    assert app.db.list_videos() == []  # мой канал не попадает в БД конкурентов


def test_me_requires_channel(app, now):
    with pytest.raises(ValueError):
        my_channel_report(app.youtube, app.profile, app.config, now)
    assert "не задан channel" in cmd_me(app, now)


def test_me_cli(cli_app):
    cli_app.profile = cli_app.profile.model_copy(update={"channel": "@smallbuilder"})
    r = CliRunner().invoke(cli, ["me", "--top", "3"])
    assert r.exit_code == 0 and "Small Builder" in r.output and "sbBREAKOUT1" in r.output


def test_profile_channel_does_not_invalidate_cache(app):
    v = None
    from radar.schemas import Video, VideoFormat

    v = Video(
        id="v",
        channel_id="c",
        title="t",
        duration_sec=60,
        format=VideoFormat.SHORT,
        published_at=__import__("datetime").datetime(2026, 1, 1),
    )
    p2 = app.profile.model_copy(update={"channel": "@me"})
    assert input_hash(v, app.profile, "m", "s") == input_hash(v, p2, "m", "s")


def test_planned_ideas_and_niche_in_prompt(app, fake_llm, now):
    for ref in ("@techguru",):
        ch = app.youtube.resolve_channel(
            ref, purpose="t", now=now, niche_ids=["ai-tools"], status=ChannelStatus.WATCHING
        )
        app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)
    an = Analyzer(app.db, app.youtube, app.llm, app.config, app.profile)
    an.analyze("tgLONGOUT01", now)
    handle_feedback(app, "t", "tgLONGOUT01", now)
    an.analyze("tgSHORTOUT1", now)
    payload = json.loads(
        fake_llm.calls[-1]["prompt"].split("(JSON):\n", 1)[1].rsplit("\n\nПревью", 1)[0]
    )
    assert payload["already_planned_ideas"] == [app.db.list_topic_suggestions()[0].title]
    assert payload["niches"] == [{"id": "ai-tools", "name": "AI-инструменты для работы"}]
    assert "channel" not in payload["my_channel"]
