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
