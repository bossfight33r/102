"""Популярное и поиск ниш: оценка тем, чарт YouTube, квота, LLM-подниши, CLI и бот."""

import json
from datetime import timedelta

import pytest
import yaml
from typer.testing import CliRunner

from radar.bot.handlers import cmd_explore, cmd_popular
from radar.cli import app as cli
from radar.collect.explore import (
    expand_topic,
    explore_cost,
    explore_topics,
    popular_chart,
    popular_search,
    score_niche,
)
from radar.config import QuotaConfig
from radar.llm.fake import FakeLLM
from radar.schemas import VideoFormat
from radar.youtube.client import YouTube
from radar.youtube.quota import QuotaPlanner
from synth import iso_z

runner = CliRunner()


def channel(cid, subs):
    return {
        "id": cid,
        "snippet": {"title": f"Канал {cid}"},
        "statistics": {"subscriberCount": str(subs)},
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}},
    }


def video(vid, cid, views, now, age_days=5, dur="PT10M", likes=None):
    pub = now - timedelta(days=age_days)
    return {
        "id": vid,
        "snippet": {"publishedAt": iso_z(pub), "channelId": cid, "title": f"Ролик {vid}"},
        "contentDetails": {"duration": dur},
        "statistics": {
            "viewCount": str(views),
            "likeCount": str(likes if likes is not None else views // 20),
        },
    }


def hit(v):
    return {
        "id": {"videoId": v["id"]},
        "snippet": {
            "channelId": v["snippet"]["channelId"],
            "publishedAt": v["snippet"]["publishedAt"],
            "title": v["snippet"]["title"],
        },
    }


@pytest.fixture
def world(fake_yt, now):
    """small-тема: 10 роликов малых каналов выше их подписчиков; giants-тема: 10 роликов гигантов."""
    small, giants = [], []
    for i in range(10):
        fake_yt.channels[f"S{i}"] = channel(f"S{i}", 5_000)
        small.append(video(f"sv{i}", f"S{i}", 20_000 + i * 100, now))
        fake_yt.channels[f"G{i}"] = channel(f"G{i}", 2_000_000)
        giants.append(video(f"gv{i}", f"G{i}", 500_000 + i * 1000, now))
    for v in small + giants:
        fake_yt.videos[v["id"]] = v
    fake_yt.search["малые каналы"] = [hit(v) for v in small]
    fake_yt.search["гиганты"] = [hit(v) for v in giants]
    fake_yt.search["пусто"] = []
    return fake_yt


def test_small_channel_niche_beats_giants(app, world, now):
    scores = explore_topics(
        app.youtube,
        ["гиганты", "пусто", "малые каналы"],
        app.config,
        now,
        region="RU",
        language="ru",
    )
    assert [s.topic for s in scores] == ["малые каналы", "гиганты", "пусто"]
    small, giants, empty = scores
    assert (
        small.breakout_share == 1.0
        and small.small_channel_share == 1.0
        and small.big_channel_share == 0
    )
    assert giants.big_channel_share == 1.0 and giants.breakout_share == 0
    assert giants.median_views_per_day > small.median_views_per_day  # спрос у гигантов выше…
    assert small.score > giants.score  # …но малому каналу шанс только в первой нише
    assert "малые каналы выстреливают" in small.note and "крупные каналы" in giants.note
    assert empty.n_videos == 0 and empty.score == 0
    assert len(small.examples) == 3 and small.examples[0].subs == 5000


def test_quota_accounting_and_cost(app, world, now):
    explore_topics(
        app.youtube, ["малые каналы", "гиганты"], app.config, now, region=None, language=None
    )
    # на тему: search 100 + videos.list 1 + channels.list 1
    assert (
        app.db.quota_used(
            __import__("radar.timeutil", fromlist=["x"]).quota_day(now), method="search.list"
        )
        == 200
    )
    assert app.planner.used(now) == 2 * 102 == explore_cost(app.config, 2)
    assert app.planner.discovery_used(now) == 204  # purpose discovery:explore — резерв discovery


def test_max_topics_cap_and_budget_deferral(db, world, app, now):
    app.config.explore.max_topics = 1
    s = explore_topics(
        app.youtube, ["малые каналы", "гиганты"], app.config, now, region=None, language=None
    )
    assert len(s) == 1
    planner = QuotaPlanner(
        db, QuotaConfig(daily_budget=360, discovery_reserve=0, safety_margin=100)
    )
    yt = YouTube(world, planner, app.config.formats)
    app.config.explore.max_topics = 5
    s = explore_topics(
        yt, ["малые каналы", "гиганты", "пусто"], app.config, now, region=None, language=None
    )
    assert [x.topic for x in s] == [
        "малые каналы"
    ]  # на второй search квоты нет — отдали посчитанное


def test_format_filter(app, world, now):
    world.videos["sv0"]["contentDetails"]["duration"] = "PT30S"
    s = explore_topics(
        app.youtube,
        ["малые каналы"],
        app.config,
        now,
        region=None,
        language=None,
        fmt=VideoFormat.SHORT,
    )
    assert s[0].n_videos == 1 and s[0].shorts_share == 1.0
    assert world.calls_of("search.list")[-1]["video_duration"] == "short"


def test_popular_chart_and_search(app, world, now):
    rows = popular_chart(app.youtube, app.config, now, region="RU")
    assert rows and rows[0].views_per_day >= rows[-1].views_per_day
    assert all(r.url.startswith("https://youtu.be/") for r in rows)
    assert world.calls_of("videos.mostPopular")[0]["region_code"] == "RU"
    assert len(rows) <= app.config.explore.popular_limit

    rows = popular_search(
        app.youtube, "малые каналы", app.config, now, days=7, region="RU", language="ru"
    )
    top = rows[0]
    assert (
        top.subs == 5000 and top.subs_ratio >= 4 and top.like_rate == pytest.approx(0.05, abs=0.01)
    )
    assert top.views_per_day == pytest.approx(top.views / 5, rel=0.01)


def test_chart_cost_is_cheap(app, world, now):
    popular_chart(app.youtube, app.config, now, region="RU")
    assert app.planner.used(now) <= 3  # mostPopular 1 ед. + channels.list 1–2 ед.


def test_score_handles_unknown_subs(app, now):
    from radar.schemas import PopularVideo

    rows = [
        PopularVideo(
            video_id=f"v{i}",
            title="t",
            channel_id="c",
            format=VideoFormat.LONG,
            duration_sec=600,
            age_days=3,
            views=1000,
            views_per_day=300,
            url="u",
        )
        for i in range(3)
    ]
    s = score_niche("x", rows, app.config)
    assert s.score > 0 and "мало роликов" in s.note and s.breakout_share == 0


def test_expand_topic_dedup_and_cost(app, now):
    llm = FakeLLM(
        responder=lambda s, p: json.dumps(
            {
                "topics": [
                    "excel для бухгалтеров",
                    "Excel для бухгалтеров",
                    "  power  query ",
                    "excel",
                    "",
                ]
            }
        ),
        cost=0.003,
    )
    out = expand_topic(llm, "excel", app.profile, app.config, app.db, now)
    assert out == ["excel для бухгалтеров", "power query"]  # дубль, пустое и сама тема убраны
    assert llm.calls[0]["json_mode"] is True and llm.calls[0]["effort"] == "low"
    assert (
        "my_channel" in llm.calls[0]["prompt"]
        and "channel" not in json.loads(llm.calls[0]["prompt"])["my_channel"]
    )
    assert app.db.llm_cost_since(now - timedelta(hours=1)) == pytest.approx(0.003)


def test_cli_explore_and_popular(cli_app, world, now):
    r = runner.invoke(cli, ["explore", "малые каналы", "гиганты"])
    assert r.exit_code == 0, r.output
    assert "≈204 ед. квоты" in r.output and r.output.index("1. малые каналы") < r.output.index(
        "2. гиганты"
    )
    path = cli_app.settings.exports_dir / "niche_explore.yaml"
    data = yaml.safe_load(path.read_text())
    assert data["niches"][0]["topic"] == "малые каналы"

    r = runner.invoke(cli, ["popular"])
    assert r.exit_code == 0 and "Топ YouTube · RU" in r.output and "/сут" in r.output
    r = runner.invoke(cli, ["popular", "малые каналы", "--days", "10"])
    assert "Популярное по теме «малые каналы» за 10 дн." in r.output and "×4" in r.output


def test_cli_explore_expand(cli_app, world, now):
    cli_app._llm = FakeLLM(
        responder=lambda s, p: json.dumps({"topics": ["малые каналы", "гиганты"]})
    )
    r = runner.invoke(cli, ["explore", "широкая тема", "--expand"])
    assert r.exit_code == 0, r.output
    assert "Подниши от LLM: малые каналы; гиганты" in r.output and "1. малые каналы" in r.output
    cli_app.config.analysis.enabled = False
    r = runner.invoke(cli, ["explore", "тема", "--expand"])
    assert r.exit_code == 1 and "нужен LLM" in r.output


def test_bot_commands(app, world, now):
    assert "Использование" in cmd_explore(app, "", now)
    text = cmd_explore(app, "малые каналы, гиганты", now)
    assert "<b>1. малые каналы</b>" in text and "≈204 ед. квоты" in text and len(text) <= 4096
    assert "Популярное по теме" in cmd_popular(app, "малые каналы", now)
    assert "Топ YouTube" in cmd_popular(app, "", now)
    app._llm = FakeLLM(responder=lambda s, p: json.dumps({"topics": ["малые каналы"]}))
    assert "Подниши от LLM: малые каналы" in cmd_explore(app, "что-то", now, expand=True)
