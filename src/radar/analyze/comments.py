"""Топ комментариев для анализа (commentThreads.list, 1 ед.)."""

from __future__ import annotations

from datetime import datetime

from radar.schemas import Comment
from radar.youtube.client import YouTube


def fetch_top_comments(yt: YouTube, video_id: str, n: int, now: datetime) -> list[Comment]:
    comments = yt.top_comments(video_id, n, purpose="analysis", now=now)
    return sorted(comments, key=lambda c: c.likes, reverse=True)[:n]


def comments_for_prompt(comments: list[Comment], max_chars: int = 400) -> list[dict[str, object]]:
    return [{"likes": c.likes, "text": c.text[:max_chars]} for c in comments]
