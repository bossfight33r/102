"""Ежедневный бэкап SQLite (онлайн-копия через sqlite3 backup API) с ротацией."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from radar.db import Database
from radar.schemas import TaskResult
from radar.timeutil import ensure_utc

PREFIX = "radar-"


def backup_path(backup_dir: Path, now: datetime) -> Path:
    return backup_dir / f"{PREFIX}{ensure_utc(now):%Y%m%d}.db"


def backup_due(backup_dir: Path, now: datetime) -> bool:
    return not backup_path(backup_dir, now).exists()


def backup_db(db: Database, backup_dir: Path, now: datetime, keep: int) -> TaskResult:
    """Копия БД на сегодня (UTC) + удаление старых сверх keep. Безопасно при работающем боте (WAL)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_path(backup_dir, now)
    tmp = target.with_suffix(".tmp")
    dst = sqlite3.connect(tmp)
    try:
        db.conn.backup(dst)
    finally:
        dst.close()
    tmp.replace(target)
    old = sorted(backup_dir.glob(f"{PREFIX}*.db"))[:-keep] if keep > 0 else []
    for p in old:
        p.unlink()
    return TaskResult(
        name="backup",
        stats={"file": target.name, "size_kb": target.stat().st_size // 1024, "removed": len(old)},
    )
