"""Вопросы зрителей, CSV-экспорт, флуд-контроль Telegram, кандидаты."""

import csv
from datetime import timedelta

from aiogram.exceptions import TelegramRetryAfter
from typer.testing import CliRunner

from radar.analyze.analyzer import Analyzer
from radar.bot.handlers import cmd_candidates, cmd_questions
from radar.bot.notifier import TelegramNotifier
from radar.cli import app as cli
from radar.collect.discovery import run_discovery
from radar.collect.watchlist import poll_watchlist
from radar.export.suggestions import export_outliers_csv
from radar.schemas import ChannelStatus, OutMessage
from radar.score.outliers import score_all
from radar.trends import audience_questions, send_weekly_report
from test_bot import FakeSession


def ready(app, now):
    for ref in ("@techguru", "@smallbuilder"):
        ch = app.youtube.resolve_channel(
            ref, purpose="t", now=now, niche_ids=["ai-tools"], status=ChannelStatus.WATCHING
        )
        app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)
    Analyzer(app.db, app.youtube, app.llm, app.config, app.profile).analyze_pending(now)


def test_audience_questions_dedup(app, now):
    ready(app, now)
    qs = audience_questions(app.db, now + timedelta(hours=1), 7)
    texts = [q["question"] for q in qs["ai-tools"]]
    # FakeLLM даёт одинаковые вопросы для всех трёх роликов — остаются уникальные
    assert texts == ["Работает ли это бесплатно?", "Как настроить на Mac?"]
    assert qs["ai-tools"][0]["video_id"] in {"tgLONGOUT01", "tgSHORTOUT1", "sbBREAKOUT1"}
    assert audience_questions(app.db, now + timedelta(days=30), 7) == {}
    assert "Работает ли это бесплатно?" in cmd_questions(app, now + timedelta(hours=1))


def test_weekly_report_includes_questions(app, notifier, now):
    ready(app, now)
    send_weekly_report(app, now + timedelta(hours=1))
    assert any("Вопросы зрителей" in m.text for m in notifier.sent)
    assert (app.settings.exports_dir / "audience_questions.yaml").exists()


def test_outliers_csv(app, now, tmp_path):
    ready(app, now)
    path = tmp_path / "o.csv"
    assert export_outliers_csv(app.db, path, now - timedelta(days=1)) == 3
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("﻿")
    rows = list(csv.DictReader(raw.lstrip("﻿").splitlines()))
    assert {r["url"] for r in rows} == {
        f"https://youtu.be/{v}" for v in ("tgLONGOUT01", "tgSHORTOUT1", "sbBREAKOUT1")
    }
    assert all(r["idea"] for r in rows) and float(rows[0]["score"]) >= float(rows[-1]["score"])


class FloodSession(FakeSession):
    def __init__(self, floods):
        super().__init__()
        self.floods = floods

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if self.floods:
            self.floods -= 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=7)
        return True


def test_flood_wait_and_retry():
    session = FloodSession(floods=2)
    n = TelegramNotifier("123456:" + "A" * 35, [1], session=session)
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    n._sleep = fake_sleep
    assert n.send([OutMessage(text="a"), OutMessage(text="b")]) == 2
    assert waits == [7, 7] and len(session.requests) == 4


def test_candidates_sorted_and_more(app, now):
    run_discovery(app.youtube, app.db, app.config, app.db.list_niches(), now)
    msgs = cmd_candidates(app, limit=2)
    assert "ещё 1" in msgs[0].text
    subs = [
        int(c.subs or 0)
        for c in sorted(
            app.db.list_channels(status=ChannelStatus.CANDIDATE), key=lambda c: -(c.subs or 0)
        )
    ]
    assert "AI Practice" in msgs[1].text and subs[0] == 120_000 or subs[0] >= subs[1]


def test_cli_export_and_questions(cli_app, now):
    runner = CliRunner()
    r = runner.invoke(cli, ["tick"])
    assert r.exit_code == 0
    r = runner.invoke(cli, ["export", "--days", "30"])
    assert r.exit_code == 0 and "2 аутлайеров" in r.output
    r = runner.invoke(cli, ["questions"])
    assert r.exit_code == 0 and "Вопросы зрителей" in r.output
