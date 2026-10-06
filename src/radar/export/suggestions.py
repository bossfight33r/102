"""Экспорт в data/exports: темы, отмеченные кнопкой «В темы», и подсказки по контенту."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from radar.db import Database
from radar.schemas import ChannelProfile, TopicSuggestion

TOPICS_FILE = "topic_suggestions.yaml"
HINTS_FILE = "content_hints.yaml"


def write_yaml(path: Path, data: Any) -> None:
    """Атомарная запись: временный файл + rename (файл не бывает полузаписанным)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=100)
    os.replace(tmp, path)


def write_text(path: Path, text: str) -> None:
    """Атомарная запись текстового файла."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def suggestion_from_analysis(
    db: Database, video_id: str, profile: ChannelProfile | None
) -> TopicSuggestion | None:
    analysis = db.get_analysis(video_id)
    if analysis is None:
        return None
    idea = analysis.idea_for_my_channel
    w = analysis.why_it_worked
    outlier = db.get_outlier(video_id)
    why = f"{w.title_pattern}; {w.topic}"
    if outlier:
        why = f"×{outlier.ratio:.1f} к медиане канала. {why}"
    key_points = list(idea.key_points)
    key_points += [f"Вопрос зрителей: {q}" for q in analysis.audience_questions[:3]]
    return TopicSuggestion(
        title=idea.title,
        why=why,
        source_video_ids=[video_id],
        audience_level=profile.audience_level if profile else "",
        key_points=key_points,
    )


def add_to_topics(
    db: Database, video_id: str, profile: ChannelProfile | None, exports_dir: Path, now: datetime
) -> TopicSuggestion | None:
    """Идемпотентно: повторное нажатие не дублирует тему. None — анализа ещё нет."""
    ts = suggestion_from_analysis(db, video_id, profile)
    if ts is None:
        return None
    db.add_topic_suggestion(video_id, ts, now)
    write_topic_suggestions(db, exports_dir)
    return ts


def write_topic_suggestions(db: Database, exports_dir: Path) -> Path:
    path = exports_dir / TOPICS_FILE
    write_yaml(path, {"topics": [t.model_dump(mode="json") for t in db.list_topic_suggestions()]})
    return path


CSV_COLUMNS = [
    "detected_at",
    "niche",
    "channel",
    "subs",
    "title",
    "url",
    "format",
    "duration_sec",
    "published_at",
    "views",
    "ratio",
    "z_score",
    "velocity_ratio",
    "score",
    "flags",
    "title_pattern",
    "topic",
    "hook",
    "idea",
    "difference",
    "short_form_angle",
]


def export_outliers_csv(db: Database, path: Path, since: datetime) -> int:
    """Аутлайеры с анализом в CSV (UTF-8 с BOM — корректно открывается в Excel/Numbers)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    rows = 0
    for o in db.list_outliers(since=since):
        video, channel, a = (
            db.get_video(o.video_id),
            db.get_channel(o.channel_id),
            db.get_analysis(o.video_id),
        )
        writer.writerow(
            {
                "detected_at": o.detected_at.isoformat(),
                "niche": ",".join(channel.niche_ids) if channel else "",
                "channel": channel.title if channel else o.channel_id,
                "subs": channel.subs if channel else "",
                "title": video.title if video else "",
                "url": f"https://youtu.be/{o.video_id}",
                "format": o.format.value,
                "duration_sec": video.duration_sec if video else "",
                "published_at": video.published_at.isoformat() if video else "",
                "views": o.views,
                "ratio": o.ratio,
                "z_score": o.z_score,
                "velocity_ratio": o.velocity_ratio if o.velocity_ratio is not None else "",
                "score": o.score,
                "flags": ",".join(o.reason_flags),
                "title_pattern": a.why_it_worked.title_pattern if a else "",
                "topic": a.why_it_worked.topic if a else "",
                "hook": a.hook_formula if a else "",
                "idea": a.idea_for_my_channel.title if a else "",
                "difference": a.idea_for_my_channel.difference_from_original if a else "",
                "short_form_angle": a.short_form_angle if a else "",
            }
        )
        rows += 1
    write_text(path, "﻿" + buf.getvalue())
    return rows
