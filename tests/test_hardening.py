"""Регрессии второго круга: миграции, пропавшие видео, пустые плейлисты, ожидание анализа, алерты."""

import sqlite3
from datetime import timedelta

from radar.analyze.analyzer import Analyzer
from radar.collect.snapshots import collect_snapshots, due_video_ids
from radar.collect.watchlist import poll_watchlist
from radar.db import _SCHEMA, Database
from radar.digest.build import build_digest, digest_due, send_digest
from radar.schemas import ChannelStatus, TaskResult
from radar.score.outliers import score_all
from radar.tick import Task, run_tick


def watch(app, now, *refs):
    for ref in refs:
        ch = app.youtube.resolve_channel(
            ref, purpose="t", now=now, niche_ids=["ai-tools"], status=ChannelStatus.WATCHING
        )
        app.db.upsert_channel(ch, now, keep_status=False)


def test_migration_v1_to_v2(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO kv(key, value) VALUES ('k', 'v')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version == 2 and db.get_kv("k") == "v"
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(videos)")}
    assert "gone_at" in cols
    db.close()
    assert Database(path).schema_version == 2  # повторное открытие — без ошибок


def test_gone_videos_not_polled_again(app, fake_yt, now):
    watch(app, now, "@techguru")
    poll_watchlist(app.youtube, app.db, app.config, now)
    del fake_yt.videos["tgLONGOUT01"]  # видео удалили/скрыли
    t = now + timedelta(hours=24)
    r = collect_snapshots(app.youtube, app.db, app.config, t)
    assert r.stats["gone"] == 1
    assert "tgLONGOUT01" not in due_video_ids(app.db, app.config, t + timedelta(days=2))


def test_missing_playlist_does_not_burn_quota(app, fake_yt, now):
    watch(app, now, "@techguru")
    del fake_yt.playlists["UUtechguru00000000000000"]
    poll_watchlist(app.youtube, app.db, app.config, now)
    poll_watchlist(app.youtube, app.db, app.config, now + timedelta(minutes=15))
    assert len(fake_yt.calls_of("playlistItems.list")) == 1


def _ready(app, now):
    watch(app, now, "@techguru", "@smallbuilder")
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)


def test_hidden_channel_not_analyzed(app, now):
    _ready(app, now)
    app.db.set_channel_status("UCsmallbuilder0000000000", ChannelStatus.HIDDEN)
    pending = Analyzer(app.db, app.youtube, app.llm, app.config, app.profile).pending(now)
    assert "sbBREAKOUT1" not in pending and "tgLONGOUT01" in pending


def test_preview_then_send_rebuilds_with_analysis(app, notifier, now):
    _ready(app, now)
    preview = build_digest(app.db, app.config, now)  # /digest до анализа
    assert all(i.idea is None for i in preview.items)
    Analyzer(app.db, app.youtube, app.llm, app.config, app.profile).analyze_pending(now)
    send_digest(app.db, app.config, notifier, now)
    sent = app.db.get_digest(preview.date)
    assert sent.sent_at and all(i.idea for i in sent.items)


def test_digest_waits_for_analysis(app, now):
    # now = 09:00 MSK, send_hour 8, ожидание 2 ч
    assert not digest_due(app.db, app.config, now, analysis_pending=True)
    assert digest_due(app.db, app.config, now + timedelta(hours=1), analysis_pending=True)
    assert digest_due(app.db, app.config, now, analysis_pending=False)


def test_task_error_alert_once_per_day(app, notifier, now):
    def boom(a, n):
        raise RuntimeError("key=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456 сломалось")

    def alerts(a, n):
        pending = a.db.pending_alerts()
        a.notifier.send(
            [
                __import__("radar.schemas", fromlist=["OutMessage"]).OutMessage(text=t)
                for _, t in pending
            ]
        )
        for i, _ in pending:
            a.db.mark_alert_sent(i, n)
        return TaskResult(name="alerts")

    tasks = [Task("bad", lambda a, n: True, boom), Task("alerts", lambda a, n: True, alerts)]
    run_tick(app, now, tasks=tasks)
    run_tick(app, now + timedelta(minutes=15), tasks=tasks)
    texts = [m.text for m in notifier.sent]
    assert len(texts) == 1 and "bad" in texts[0] and "AIza" not in texts[0]
