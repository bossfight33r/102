"""SQLite — source of truth. Снимки статистики append-only (защищены триггерами)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from radar.schemas import (
    Analysis,
    Channel,
    ChannelBaseline,
    ChannelStatus,
    Digest,
    Feedback,
    FeedbackAction,
    Niche,
    Outlier,
    QuotaEntry,
    TopicSuggestion,
    Video,
    VideoFormat,
    VideoSnapshot,
)
from radar.timeutil import iso, parse_dt

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS niches (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    handle TEXT,
    subs INTEGER,
    uploads_playlist_id TEXT NOT NULL,
    niche_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    added_at TEXT NOT NULL,
    last_polled_at TEXT,
    subs_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_channels_status ON channels(status);
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL,
    duration_sec INTEGER NOT NULL,
    format TEXT NOT NULL,
    published_at TEXT NOT NULL,
    thumbnail_url TEXT,
    chapters TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_videos_channel ON videos(channel_id, format, published_at);
CREATE TABLE IF NOT EXISTS video_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    views INTEGER NOT NULL,
    likes INTEGER,
    comments INTEGER,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_video ON video_snapshots(video_id, collected_at);
CREATE TRIGGER IF NOT EXISTS snapshots_no_update BEFORE UPDATE ON video_snapshots
BEGIN SELECT RAISE(ABORT, 'video_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete BEFORE DELETE ON video_snapshots
BEGIN SELECT RAISE(ABORT, 'video_snapshots is append-only'); END;
CREATE TABLE IF NOT EXISTS channel_baselines (
    channel_id TEXT NOT NULL,
    format TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (channel_id, format)
);
CREATE TABLE IF NOT EXISTS outliers (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    format TEXT NOT NULL,
    score REAL NOT NULL,
    data TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outliers_detected ON outliers(detected_at);
CREATE TABLE IF NOT EXISTS analyses (
    video_id TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost REAL NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    method TEXT NOT NULL,
    units INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quota_date ON quota_ledger(date);
CREATE TABLE IF NOT EXISTS digests (
    date TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_suggestions (
    video_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_runs (
    name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
"""


# v2: видео, которые videos.list перестал возвращать (удалены/приватны), не опрашиваются.
_MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA),
    (2, "ALTER TABLE videos ADD COLUMN gone_at TEXT;"),
]
assert _MIGRATIONS[-1][0] == SCHEMA_VERSION


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _opt_dt(value: str | None) -> datetime | None:
    return parse_dt(value) if value else None


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def _migrate(self) -> None:
        """Последовательные миграции по PRAGMA user_version."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for target, script in _MIGRATIONS:
            if version < target:
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {target}")
                version = target

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    # --- ниши ---------------------------------------------------------------

    def upsert_niche(self, niche: Niche, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO niches(id, data, enabled, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, enabled=excluded.enabled, "
            "updated_at=excluded.updated_at",
            (niche.id, niche.model_dump_json(), int(niche.enabled), iso(now)),
        )

    def get_niche(self, niche_id: str) -> Niche | None:
        row = self._one("SELECT data FROM niches WHERE id=?", (niche_id,))
        return Niche.model_validate_json(row["data"]) if row else None

    def list_niches(self, enabled_only: bool = False) -> list[Niche]:
        sql = (
            "SELECT data FROM niches"
            + (" WHERE enabled=1" if enabled_only else "")
            + " ORDER BY id"
        )
        return [Niche.model_validate_json(r["data"]) for r in self._all(sql)]

    # --- каналы -------------------------------------------------------------

    def _row_to_channel(self, r: sqlite3.Row) -> Channel:
        return Channel(
            id=r["id"],
            title=r["title"],
            handle=r["handle"],
            subs=r["subs"],
            uploads_playlist_id=r["uploads_playlist_id"],
            niche_ids=json.loads(r["niche_ids"]),
            status=ChannelStatus(r["status"]),
            added_at=parse_dt(r["added_at"]),
        )

    def upsert_channel(self, ch: Channel, now: datetime, *, keep_status: bool = True) -> Channel:
        """Создаёт канал или обновляет метаданные. Ниши объединяются, статус сохраняется."""
        existing = self.get_channel(ch.id)
        if existing:
            niche_ids = sorted(set(existing.niche_ids) | set(ch.niche_ids))
            status = existing.status if keep_status else ch.status
            self.conn.execute(
                "UPDATE channels SET title=?, handle=?, subs=?, uploads_playlist_id=?, niche_ids=?, "
                "status=?, subs_updated_at=? WHERE id=?",
                (
                    ch.title,
                    ch.handle or existing.handle,
                    ch.subs if ch.subs is not None else existing.subs,
                    ch.uploads_playlist_id,
                    _j(niche_ids),
                    status.value,
                    iso(now),
                    ch.id,
                ),
            )
        else:
            self.conn.execute(
                "INSERT INTO channels(id, title, handle, subs, uploads_playlist_id, niche_ids, status, "
                "added_at, subs_updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ch.id,
                    ch.title,
                    ch.handle,
                    ch.subs,
                    ch.uploads_playlist_id,
                    _j(sorted(set(ch.niche_ids))),
                    ch.status.value,
                    iso(ch.added_at),
                    iso(now),
                ),
            )
        result = self.get_channel(ch.id)
        assert result is not None
        return result

    def get_channel(self, channel_id: str) -> Channel | None:
        row = self._one("SELECT * FROM channels WHERE id=?", (channel_id,))
        return self._row_to_channel(row) if row else None

    def list_channels(
        self, status: ChannelStatus | None = None, niche_id: str | None = None
    ) -> list[Channel]:
        sql, params = "SELECT * FROM channels", []
        if status:
            sql += " WHERE status=?"
            params.append(status.value)
        chans = [self._row_to_channel(r) for r in self._all(sql + " ORDER BY added_at, id", params)]
        if niche_id:
            chans = [c for c in chans if niche_id in c.niche_ids]
        return chans

    def channel_ids(self) -> set[str]:
        return {r["id"] for r in self._all("SELECT id FROM channels")}

    def add_channel_niche(self, channel_id: str, niche_id: str) -> bool:
        """Добавить нишу известному каналу. True — ниша была новой."""
        ch = self.get_channel(channel_id)
        if ch is None or niche_id in ch.niche_ids:
            return False
        self.conn.execute(
            "UPDATE channels SET niche_ids=? WHERE id=?",
            (_j(sorted({*ch.niche_ids, niche_id})), channel_id),
        )
        return True

    def set_channel_status(self, channel_id: str, status: ChannelStatus) -> bool:
        cur = self.conn.execute(
            "UPDATE channels SET status=? WHERE id=?", (status.value, channel_id)
        )
        return cur.rowcount > 0

    def channel_poll_times(self) -> dict[str, tuple[datetime | None, datetime | None]]:
        rows = self._all("SELECT id, last_polled_at, subs_updated_at FROM channels")
        return {
            r["id"]: (_opt_dt(r["last_polled_at"]), _opt_dt(r["subs_updated_at"])) for r in rows
        }

    def set_channel_polled(self, channel_id: str, at: datetime) -> None:
        self.conn.execute("UPDATE channels SET last_polled_at=? WHERE id=?", (iso(at), channel_id))

    def update_channel_subs(self, channel_id: str, subs: int | None, at: datetime) -> None:
        self.conn.execute(
            "UPDATE channels SET subs=COALESCE(?, subs), subs_updated_at=? WHERE id=?",
            (subs, iso(at), channel_id),
        )

    # --- видео --------------------------------------------------------------

    def _row_to_video(self, r: sqlite3.Row) -> Video:
        return Video(
            id=r["id"],
            channel_id=r["channel_id"],
            title=r["title"],
            description=r["description"],
            tags=json.loads(r["tags"]),
            duration_sec=r["duration_sec"],
            format=VideoFormat(r["format"]),
            published_at=parse_dt(r["published_at"]),
            thumbnail_url=r["thumbnail_url"],
            chapters=json.loads(r["chapters"]),
        )

    def upsert_video(self, v: Video, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO videos(id, channel_id, title, description, tags, duration_sec, format, "
            "published_at, thumbnail_url, chapters, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description, "
            "tags=excluded.tags, duration_sec=excluded.duration_sec, format=excluded.format, "
            "thumbnail_url=excluded.thumbnail_url, chapters=excluded.chapters",
            (
                v.id,
                v.channel_id,
                v.title,
                v.description,
                _j(v.tags),
                v.duration_sec,
                v.format.value,
                iso(v.published_at),
                v.thumbnail_url,
                _j([c.model_dump() for c in v.chapters]),
                iso(now),
            ),
        )

    def get_video(self, video_id: str) -> Video | None:
        row = self._one("SELECT * FROM videos WHERE id=?", (video_id,))
        return self._row_to_video(row) if row else None

    def existing_video_ids(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            found |= {
                r["id"] for r in self._all(f"SELECT id FROM videos WHERE id IN ({marks})", chunk)
            }
        return found

    def list_videos(
        self,
        channel_id: str | None = None,
        fmt: VideoFormat | None = None,
        published_after: datetime | None = None,
    ) -> list[Video]:
        sql, params = "SELECT * FROM videos WHERE 1=1", []
        if channel_id:
            sql += " AND channel_id=?"
            params.append(channel_id)
        if fmt:
            sql += " AND format=?"
            params.append(fmt.value)
        if published_after:
            sql += " AND published_at>=?"
            params.append(iso(published_after))
        return [
            self._row_to_video(r) for r in self._all(sql + " ORDER BY published_at DESC", params)
        ]

    def mark_videos_gone(self, video_ids: Iterable[str], at: datetime) -> None:
        self.conn.executemany(
            "UPDATE videos SET gone_at=? WHERE id=? AND gone_at IS NULL",
            [(iso(at), v) for v in video_ids],
        )

    def videos_for_snapshots(
        self, published_after: datetime
    ) -> list[tuple[str, datetime, datetime | None]]:
        """(video_id, published_at, last_snapshot_at) видео каналов в статусе watching."""
        rows = self._all(
            "SELECT v.id, v.published_at, MAX(s.collected_at) AS last_at FROM videos v "
            "JOIN channels c ON c.id = v.channel_id AND c.status = 'watching' "
            "LEFT JOIN video_snapshots s ON s.video_id = v.id "
            "WHERE v.published_at >= ? AND v.gone_at IS NULL GROUP BY v.id ORDER BY v.published_at DESC",
            (iso(published_after),),
        )
        return [(r["id"], parse_dt(r["published_at"]), _opt_dt(r["last_at"])) for r in rows]

    # --- снимки (append-only) -------------------------------------------------

    def add_snapshots(self, snaps: Iterable[VideoSnapshot]) -> int:
        rows = [(s.video_id, s.views, s.likes, s.comments, iso(s.collected_at)) for s in snaps]
        with self.tx():
            self.conn.executemany(
                "INSERT INTO video_snapshots(video_id, views, likes, comments, collected_at) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def snapshots_for(self, video_id: str) -> list[VideoSnapshot]:
        rows = self._all(
            "SELECT * FROM video_snapshots WHERE video_id=? ORDER BY collected_at, id", (video_id,)
        )
        return [self._row_to_snapshot(r) for r in rows]

    def snapshots_by_video(
        self, video_ids: Iterable[str] | None = None
    ) -> dict[str, list[VideoSnapshot]]:
        out: dict[str, list[VideoSnapshot]] = {}
        rows = self._all("SELECT * FROM video_snapshots ORDER BY collected_at, id")
        wanted = set(video_ids) if video_ids is not None else None
        for r in rows:
            if wanted is None or r["video_id"] in wanted:
                out.setdefault(r["video_id"], []).append(self._row_to_snapshot(r))
        return out

    def latest_snapshots(self) -> dict[str, VideoSnapshot]:
        rows = self._all(
            "SELECT s.* FROM video_snapshots s JOIN ("
            " SELECT video_id, MAX(id) AS mid FROM video_snapshots GROUP BY video_id"
            ") m ON s.id = m.mid"
        )
        return {r["video_id"]: self._row_to_snapshot(r) for r in rows}

    def count_snapshots(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM video_snapshots")["n"]  # type: ignore[index]

    @staticmethod
    def _row_to_snapshot(r: sqlite3.Row) -> VideoSnapshot:
        return VideoSnapshot(
            video_id=r["video_id"],
            views=r["views"],
            likes=r["likes"],
            comments=r["comments"],
            collected_at=parse_dt(r["collected_at"]),
        )

    # --- базлайны и аутлайеры -------------------------------------------------

    def upsert_baseline(self, b: ChannelBaseline) -> None:
        self.conn.execute(
            "INSERT INTO channel_baselines(channel_id, format, data) VALUES (?,?,?) "
            "ON CONFLICT(channel_id, format) DO UPDATE SET data=excluded.data",
            (b.channel_id, b.format.value, b.model_dump_json()),
        )

    def get_baseline(self, channel_id: str, fmt: VideoFormat) -> ChannelBaseline | None:
        row = self._one(
            "SELECT data FROM channel_baselines WHERE channel_id=? AND format=?",
            (channel_id, fmt.value),
        )
        return ChannelBaseline.model_validate_json(row["data"]) if row else None

    def list_baselines(self) -> list[ChannelBaseline]:
        return [
            ChannelBaseline.model_validate_json(r["data"])
            for r in self._all("SELECT data FROM channel_baselines ORDER BY channel_id, format")
        ]

    def upsert_outlier(self, o: Outlier, now: datetime) -> Outlier:
        """detected_at фиксируется при первом обнаружении, остальное обновляется."""
        row = self._one("SELECT detected_at FROM outliers WHERE video_id=?", (o.video_id,))
        if row:
            o = o.model_copy(update={"detected_at": parse_dt(row["detected_at"])})
        self.conn.execute(
            "INSERT INTO outliers(video_id, channel_id, format, score, data, detected_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET score=excluded.score, "
            "data=excluded.data, updated_at=excluded.updated_at",
            (
                o.video_id,
                o.channel_id,
                o.format.value,
                o.score,
                o.model_dump_json(),
                iso(o.detected_at),
                iso(now),
            ),
        )
        return o

    def get_outlier(self, video_id: str) -> Outlier | None:
        row = self._one("SELECT data FROM outliers WHERE video_id=?", (video_id,))
        return Outlier.model_validate_json(row["data"]) if row else None

    def list_outliers(
        self,
        since: datetime | None = None,
        fmt: VideoFormat | None = None,
        min_score: float | None = None,
        limit: int | None = None,
    ) -> list[Outlier]:
        sql, params = "SELECT data FROM outliers WHERE 1=1", []
        if since:
            sql += " AND detected_at>=?"
            params.append(iso(since))
        if fmt:
            sql += " AND format=?"
            params.append(fmt.value)
        if min_score is not None:
            sql += " AND score>=?"
            params.append(min_score)
        sql += " ORDER BY score DESC, video_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [Outlier.model_validate_json(r["data"]) for r in self._all(sql, params)]

    # --- анализ и LLM -------------------------------------------------------

    def save_analysis(self, a: Analysis) -> None:
        self.conn.execute(
            "INSERT INTO analyses(video_id, input_hash, data, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(video_id) DO UPDATE SET input_hash=excluded.input_hash, data=excluded.data, "
            "created_at=excluded.created_at",
            (a.video_id, a.input_hash, a.model_dump_json(), iso(a.created_at)),
        )

    def get_analysis(self, video_id: str) -> Analysis | None:
        row = self._one("SELECT data FROM analyses WHERE video_id=?", (video_id,))
        return Analysis.model_validate_json(row["data"]) if row else None

    def add_llm_usage(
        self,
        purpose: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        at: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage(purpose, model, input_tokens, output_tokens, cost, at) "
            "VALUES (?,?,?,?,?,?)",
            (purpose, model, input_tokens, output_tokens, cost, iso(at)),
        )

    def llm_cost_since(self, since: datetime) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(cost), 0) AS c FROM llm_usage WHERE at>=?", (iso(since),)
        )
        return float(row["c"])  # type: ignore[index]

    # --- квота --------------------------------------------------------------

    def add_quota(self, e: QuotaEntry, at: datetime) -> None:
        self.conn.execute(
            "INSERT INTO quota_ledger(date, method, units, purpose, at) VALUES (?,?,?,?,?)",
            (e.date.isoformat(), e.method, e.units, e.purpose, iso(at)),
        )

    def quota_used(
        self, day: date, purpose_prefix: str | None = None, method: str | None = None
    ) -> int:
        sql, params = (
            "SELECT COALESCE(SUM(units), 0) AS u FROM quota_ledger WHERE date=?",
            [day.isoformat()],
        )
        if purpose_prefix:
            sql += " AND purpose LIKE ?"
            params.append(purpose_prefix + "%")
        if method:
            sql += " AND method=?"
            params.append(method)
        return int(self._one(sql, params)["u"])  # type: ignore[index]

    def quota_breakdown(self, day: date) -> list[tuple[str, str, int, int]]:
        rows = self._all(
            "SELECT method, purpose, COUNT(*) AS calls, SUM(units) AS units FROM quota_ledger "
            "WHERE date=? GROUP BY method, purpose ORDER BY units DESC",
            (day.isoformat(),),
        )
        return [(r["method"], r["purpose"], r["calls"], r["units"]) for r in rows]

    def count_quota_entries(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM quota_ledger")["n"]  # type: ignore[index]

    # --- дайджест, фидбэк, темы -------------------------------------------------

    def save_digest(self, d: Digest) -> None:
        self.conn.execute(
            "INSERT INTO digests(date, data, sent_at) VALUES (?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET data=excluded.data, sent_at=excluded.sent_at",
            (d.date.isoformat(), d.model_dump_json(), iso(d.sent_at) if d.sent_at else None),
        )

    def get_digest(self, day: date) -> Digest | None:
        row = self._one("SELECT data FROM digests WHERE date=?", (day.isoformat(),))
        return Digest.model_validate_json(row["data"]) if row else None

    def digested_video_ids(self, exclude_day: date | None = None) -> set[str]:
        ids: set[str] = set()
        for r in self._all("SELECT date, data FROM digests"):
            if exclude_day and r["date"] == exclude_day.isoformat():
                continue
            ids |= {i.video_id for i in Digest.model_validate_json(r["data"]).items}
        return ids

    def add_feedback(self, f: Feedback) -> None:
        self.conn.execute(
            "INSERT INTO feedback(video_id, action, at) VALUES (?,?,?)",
            (f.video_id, f.action.value, iso(f.at)),
        )

    def list_feedback(self, action: FeedbackAction | None = None) -> list[Feedback]:
        sql, params = "SELECT * FROM feedback", []
        if action:
            sql += " WHERE action=?"
            params.append(action.value)
        return [
            Feedback(
                video_id=r["video_id"], action=FeedbackAction(r["action"]), at=parse_dt(r["at"])
            )
            for r in self._all(sql + " ORDER BY id", params)
        ]

    def add_topic_suggestion(self, video_id: str, ts: TopicSuggestion, at: datetime) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO topic_suggestions(video_id, data, added_at) VALUES (?,?,?)",
            (video_id, ts.model_dump_json(), iso(at)),
        )
        return cur.rowcount > 0

    def list_topic_suggestions(self) -> list[TopicSuggestion]:
        return [
            TopicSuggestion.model_validate_json(r["data"])
            for r in self._all("SELECT data FROM topic_suggestions ORDER BY added_at, video_id")
        ]

    # --- служебное ----------------------------------------------------------

    def get_task_run(self, name: str) -> datetime | None:
        row = self._one("SELECT last_run_at FROM task_runs WHERE name=?", (name,))
        return parse_dt(row["last_run_at"]) if row else None

    def set_task_run(self, name: str, at: datetime, status: str = "ok") -> None:
        self.conn.execute(
            "INSERT INTO task_runs(name, last_run_at, status) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET last_run_at=excluded.last_run_at, status=excluded.status",
            (name, iso(at), status),
        )

    def list_task_runs(self) -> list[tuple[str, datetime, str]]:
        return [
            (r["name"], parse_dt(r["last_run_at"]), r["status"])
            for r in self._all("SELECT * FROM task_runs ORDER BY name")
        ]

    def get_kv(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    def set_kv(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def add_alert(self, text: str, at: datetime, key: str | None = None) -> bool:
        """Алерт для бота. key делает алерт идемпотентным (повтор с тем же key игнорируется)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO alerts(key, text, created_at) VALUES (?,?,?)",
            (key, text, iso(at)),
        )
        return cur.rowcount > 0

    def pending_alerts(self) -> list[tuple[int, str]]:
        return [
            (r["id"], r["text"])
            for r in self._all("SELECT id, text FROM alerts WHERE sent_at IS NULL")
        ]

    def mark_alert_sent(self, alert_id: int, at: datetime) -> None:
        self.conn.execute("UPDATE alerts SET sent_at=? WHERE id=?", (iso(at), alert_id))
