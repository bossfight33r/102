"""radar tick: идемпотентно выполняет все задачи с наступившим сроком. Запуск — launchd каждые 15 минут."""

from __future__ import annotations

import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from radar.delivery import give_up_delivery
from radar.log import get_logger, redact_text
from radar.schemas import OutMessage, TaskResult
from radar.timeutil import iso

if TYPE_CHECKING:
    from radar.app import App

log = get_logger(__name__)

LAST_TICK_KEY = "last_tick_at"
SCORE_MARK_KEY = "score_snapshot_count"


class TickLocked(Exception):
    """Другой tick ещё работает."""


@dataclass
class Task:
    name: str
    due: Callable[[App, datetime], bool]
    run: Callable[[App, datetime], TaskResult]


# --- задачи ------------------------------------------------------------------------


def _seed_due(app: App, now: datetime) -> bool:
    from radar.collect.watchlist import seeds_pending

    return seeds_pending(app.db, app.db.list_niches(enabled_only=True), now)


def _seed(app: App, now: datetime) -> TaskResult:
    from radar.collect.watchlist import add_seed_channels

    return add_seed_channels(app.youtube, app.db, app.db.list_niches(enabled_only=True), now)


def _watchlist_due(app: App, now: datetime) -> bool:
    from radar.collect.watchlist import watchlist_due

    return watchlist_due(app.db, app.config, now)


def _watchlist(app: App, now: datetime) -> TaskResult:
    from radar.collect.watchlist import poll_watchlist

    return poll_watchlist(app.youtube, app.db, app.config, now)


def _snapshots_due(app: App, now: datetime) -> bool:
    from radar.collect.snapshots import due_video_ids

    return bool(due_video_ids(app.db, app.config, now))


def _snapshots(app: App, now: datetime) -> TaskResult:
    from radar.collect.snapshots import collect_snapshots

    return collect_snapshots(app.youtube, app.db, app.config, now)


def _discovery_due(app: App, now: datetime) -> bool:
    from radar.collect.discovery import discovery_pending

    return discovery_pending(app.youtube, app.db, app.db.list_niches(enabled_only=True), now)


def _discovery(app: App, now: datetime) -> TaskResult:
    from radar.collect.discovery import run_discovery

    return run_discovery(
        app.youtube, app.db, app.config, app.db.list_niches(enabled_only=True), now
    )


def _score_due(app: App, now: datetime) -> bool:
    return str(app.db.count_snapshots()) != app.db.get_kv(SCORE_MARK_KEY)


def _score(app: App, now: datetime) -> TaskResult:
    from radar.score.outliers import score_all

    marker = str(app.db.count_snapshots())
    result = score_all(app.db, app.config, now)
    app.db.set_kv(SCORE_MARK_KEY, marker)
    return result


def _analyze_due(app: App, now: datetime) -> bool:
    return app.has_llm() and app.profile is not None and bool(app.analyzer().pending(now))


def _analyze(app: App, now: datetime) -> TaskResult:
    return app.analyzer().analyze_pending(now)


def _digest_due(app: App, now: datetime) -> bool:
    from radar.digest.build import digest_due

    analysis_pending = False
    if (
        app.has_llm()
        and app.profile is not None
        and app.db.list_outliers(min_score=app.config.analysis.score_threshold, limit=1)
    ):
        analysis_pending = bool(app.analyzer().pending(now))
    return digest_due(app.db, app.config, now, analysis_pending=analysis_pending)


def _digest(app: App, now: datetime) -> TaskResult:
    from radar.digest.build import send_digest

    return send_digest(
        app.db,
        app.config,
        app.notifier,
        now,
        llm=app.llm if app.has_llm() else None,
        profile=app.profile,
    )


def _alerts_due(app: App, now: datetime) -> bool:
    return bool(app.db.pending_alerts())


def _alerts(app: App, now: datetime) -> TaskResult:
    pending = app.db.pending_alerts()
    delivered = app.notifier.send([OutMessage(text=text) for _, text in pending])
    if delivered == 0 and not give_up_delivery(app.db, f"alerts:{now.date().isoformat()}"):
        return TaskResult(name="alerts", stats={"sent": 0}, deferred=True)  # остаются в очереди
    for alert_id, _ in pending:
        app.db.mark_alert_sent(alert_id, now)
    return TaskResult(name="alerts", stats={"sent": len(pending)})


def _trends_due(app: App, now: datetime) -> bool:
    from radar.trends import weekly_report_due

    return weekly_report_due(app.db, app.config, now)


def _trends(app: App, now: datetime) -> TaskResult:
    from radar.trends import send_weekly_report

    return send_weekly_report(app, now)


TASKS: list[Task] = [
    Task("seed_channels", _seed_due, _seed),
    Task("watchlist", _watchlist_due, _watchlist),
    Task("snapshots", _snapshots_due, _snapshots),
    Task("discovery", _discovery_due, _discovery),
    Task("score", _score_due, _score),
    Task("analyze", _analyze_due, _analyze),
    Task("digest", _digest_due, _digest),
    Task("trends_weekly", _trends_due, _trends),
    Task("alerts", _alerts_due, _alerts),
]


# --- запуск -----------------------------------------------------------------------


@contextmanager
def tick_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TickLocked("radar tick уже выполняется") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def run_tick(app: App, now: datetime, tasks: list[Task] | None = None) -> list[TaskResult]:
    """Выполнить задачи с наступившим сроком. Ошибка одной задачи не останавливает остальные."""
    results: list[TaskResult] = []
    with tick_lock(app.settings.radar_data_dir / "tick.lock"):
        app.sync_niches(now)
        for task in tasks or TASKS:
            try:
                if not task.due(app, now):
                    continue
                result = task.run(app, now)
            except Exception as e:
                log.error("task_failed", task=task.name, error=f"{type(e).__name__}: {e}")
                app.db.set_task_run(task.name, now, "error")
                if task.name != "alerts":  # алерт об ошибке — не чаще раза в сутки на задачу
                    app.db.add_alert(
                        redact_text(f"❗ tick: задача {task.name} упала: {type(e).__name__}: {e}")[
                            :1000
                        ],
                        now,
                        key=f"task_error:{task.name}:{now.date().isoformat()}",
                    )
                results.append(
                    TaskResult(name=task.name, message=f"ошибка: {type(e).__name__}: {e}")
                )
                continue
            app.db.set_task_run(task.name, now, "deferred" if result.deferred else "ok")
            results.append(result)
        app.db.set_kv(LAST_TICK_KEY, iso(now))
    log.info("tick_done", tasks=[r.name for r in results])
    return results
