"""Объяснение скоринга, фильтр /outliers, архив дайджестов, бэкап, прогноз квоты, проверки doctor."""

import sqlite3
from datetime import timedelta

from typer.testing import CliRunner

from radar.analyze.analyzer import Analyzer
from radar.backup import backup_db, backup_due
from radar.bot.handlers import cmd_outliers, handle_feedback
from radar.cli import app as cli
from radar.collect.watchlist import poll_watchlist
from radar.digest.build import send_digest
from radar.digest.render import sparkline
from radar.schemas import ChannelStatus, VideoSnapshot
from radar.score.outliers import score_all
from radar.youtube.quota import forecast_daily_units

runner = CliRunner()


def ready(app, now):
    for ref in ("@techguru", "@smallbuilder"):
        ch = app.youtube.resolve_channel(
            ref, purpose="t", now=now, niche_ids=["ai-tools"], status=ChannelStatus.WATCHING
        )
        app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)
    Analyzer(app.db, app.youtube, app.llm, app.config, app.profile).analyze_pending(now)


def test_sparkline():
    assert sparkline([1]) == ""
    assert sparkline([0, 5, 10]) == "▁▄█"
    assert sparkline([3, 3, 3]) == "▁▁▁"
    assert len(sparkline(list(range(100)))) == 16


def test_details_include_metrics(app, now):
    ready(app, now)
    app.db.add_snapshots(
        [
            VideoSnapshot(
                video_id="tgLONGOUT01", views=300_000, collected_at=now + timedelta(hours=6)
            )
        ]
    )
    _, extra = handle_feedback(app, "d", "tgLONGOUT01", now)
    text = extra[0].text
    assert "📊 score" in text and "к медиане" in text and "Медиана канала (long)" in text
    assert "Просмотры:" in text and "→ 300.0 тыс" in text
    assert len(text) <= 4096


def test_outliers_niche_filter(app, now):
    ready(app, now)
    app.db.upsert_niche(
        app.db.get_niche("ai-tools").model_copy(update={"id": "other", "name": "O"}), now
    )
    assert "tgLONGOUT01" in cmd_outliers(app, now, "ai-tools")
    assert "нет · other" in cmd_outliers(app, now, "other")
    assert "не найдена" in cmd_outliers(app, now, "nope")


def test_digest_archive_markdown(app, notifier, now, tmp_path):
    ready(app, now)
    send_digest(app.db, app.config, notifier, now, archive_dir=tmp_path / "digests")
    md = (tmp_path / "digests" / "2026-10-06.md").read_text()
    assert md.startswith("# Дайджест 2026-10-06")
    assert (
        "## AI-инструменты для работы" in md
        and "**Идея:**" in md
        and "https://youtu.be/tgLONGOUT01" in md
    )


def test_backup_and_rotation(app, now, tmp_path):
    app.db.set_kv("marker", "1")
    d = tmp_path / "backups"
    for i in range(10):
        r = backup_db(app.db, d, now + timedelta(days=i), keep=3)
    files = sorted(p.name for p in d.glob("*.db"))
    assert files == ["radar-20261013.db", "radar-20261014.db", "radar-20261015.db"]
    assert r.stats["removed"] == 1
    conn = sqlite3.connect(d / files[-1])
    assert conn.execute("SELECT value FROM kv WHERE key='marker'").fetchone()[0] == "1"
    assert not backup_due(d, now + timedelta(days=9)) and backup_due(d, now + timedelta(days=10))


def test_quota_forecast(app, now):
    ready(app, now)
    rows = {name: units for name, units, _ in forecast_daily_units(app.db, app.config, now)}
    assert rows["watchlist"] == 2 * 24  # 2 канала × 24 опроса
    assert rows["discovery"] == 2 * 101
    assert sum(rows.values()) < app.config.quota.daily_budget


def test_cli_quota_plan_backup_doctor(cli_app, now):
    r = runner.invoke(cli, ["quota", "--plan"])
    assert r.exit_code == 0 and "ИТОГО" in r.output and "Помещается" in r.output
    r = runner.invoke(cli, ["backup"])
    assert r.exit_code == 0 and "radar-20261006.db" in r.output
    cli_app.config.watchlist.interval_minutes = 1
    for i in range(300):
        cli_app.db.upsert_channel(
            cli_app.db.get_channel("UCtechguru00000000000000").model_copy(
                update={"id": f"UC{i:022d}"}
            )
            if cli_app.db.get_channel("UCtechguru00000000000000")
            else cli_app.youtube.resolve_channel(
                "@techguru", purpose="t", now=now, status=ChannelStatus.WATCHING
            ),
            now,
            keep_status=False,
        )
    r = runner.invoke(cli, ["quota", "--plan"])
    assert "Не помещается" in r.output
    cli_app.config.digest.timezone = "Mars/Olympus"
    r = runner.invoke(cli, ["doctor"])
    assert r.exit_code == 1 and "неизвестная таймзона" in r.output
