from datetime import timedelta

import pytest

from radar.schemas import TaskResult
from radar.tick import Task, TickLocked, run_tick, tick_lock


def names(results):
    return {r.name: r for r in results}


def test_tick_full_pipeline_and_idempotent(app, fake_yt, fake_llm, notifier, now):
    """Acceptance Фазы 4: tick на фикстурах проходит весь конвейер и идемпотентен."""
    app.db.upsert_niche(app.db.get_niche("ai-tools"), now)
    r1 = names(run_tick(app, now))
    assert {"seed_channels", "watchlist", "discovery", "score", "analyze", "digest"} <= set(r1)
    assert r1["watchlist"].stats["new_videos"] == 42
    assert r1["score"].stats["outliers"] == 2
    assert r1["analyze"].stats["done"] == 2
    assert r1["digest"].stats["items"] == 2 and len(notifier.sent) == 3
    assert app.db.get_kv("last_tick_at") == now.isoformat()

    api_calls, llm_calls, sent = len(fake_yt.calls), len(fake_llm.calls), len(notifier.sent)
    snaps = app.db.count_snapshots()
    r2 = run_tick(app, now)
    assert len(fake_yt.calls) == api_calls
    assert len(fake_llm.calls) == llm_calls
    assert len(notifier.sent) == sent
    assert app.db.count_snapshots() == snaps
    assert "digest" not in names(r2) and "analyze" not in names(r2)

    # через 15 минут — тоже ничего лишнего (интервалы не наступили)
    run_tick(app, now + timedelta(minutes=15))
    assert len(fake_yt.calls) == api_calls and len(notifier.sent) == sent


def test_tick_quota_exceeded_alert(app, fake_yt, notifier, now):
    fake_yt.quota_exceeded = True
    results = names(run_tick(app, now))
    assert results["alerts"].stats["sent"] == 1
    assert any("quotaExceeded" in m.text for m in notifier.sent)
    calls = len(fake_yt.calls)
    run_tick(app, now + timedelta(minutes=15))  # пауза: в API не ходим, повторного алерта нет
    assert len(fake_yt.calls) == calls
    assert sum("quotaExceeded" in m.text for m in notifier.sent) == 1


def test_task_error_isolated(app, now):
    def boom(a, n):
        raise RuntimeError("сломалось")

    ok = Task("ok", lambda a, n: True, lambda a, n: TaskResult(name="ok"))
    results = run_tick(app, now, tasks=[Task("bad", lambda a, n: True, boom), ok])
    assert [r.name for r in results] == ["bad", "ok"]
    assert "сломалось" in results[0].message
    runs = {name: status for name, _, status in app.db.list_task_runs()}
    assert runs == {"bad": "error", "ok": "ok"}


def test_tick_lock(app, now):
    with tick_lock(app.settings.radar_data_dir / "tick.lock"), pytest.raises(TickLocked):
        run_tick(app, now)
