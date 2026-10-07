"""Фейковый YouTube-клиент на JSON-фикстурах (формат ответов YouTube Data API v3). Без сети."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from radar.timeutil import ensure_utc, parse_dt
from radar.youtube.client import QuotaExceededError, YouTubeAPIError


class FakeYouTubeClient:
    def __init__(
        self,
        *,
        channels: list[dict[str, Any]] | None = None,
        playlists: dict[str, list[dict[str, Any]]] | None = None,
        videos: list[dict[str, Any]] | None = None,
        search: dict[str, list[dict[str, Any]]] | None = None,
        comments: dict[str, list[dict[str, Any]]] | None = None,
        page_size: int = 50,
    ) -> None:
        self.channels = {c["id"]: c for c in channels or []}
        self.playlists = playlists or {}
        self.videos = {v["id"]: v for v in videos or []}
        self.search = search or {}
        self.comments = comments or {}
        self.comments_disabled: set[str] = set()
        self.page_size = page_size
        self.quota_exceeded = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @classmethod
    def from_fixtures(cls, directory: Path | str) -> FakeYouTubeClient:
        d = Path(directory)

        def load(name: str, default: Any) -> Any:
            p = d / name
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

        return cls(
            channels=load("channels.json", {"items": []})["items"],
            playlists={k: v["items"] for k, v in load("playlist_items.json", {}).items()},
            videos=load("videos.json", {"items": []})["items"],
            search={k: v["items"] for k, v in load("search.json", {}).items()},
            comments={k: v["items"] for k, v in load("comment_threads.json", {}).items()},
        )

    # --- управление состоянием в тестах -------------------------------------------

    def set_views(self, video_id: str, views: int) -> None:
        self.videos[video_id].setdefault("statistics", {})["viewCount"] = str(views)

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        return [args for m, args in self.calls if m == method]

    def _enter(self, method: str, args: dict[str, Any]) -> None:
        self.calls.append((method, args))
        if self.quota_exceeded:
            raise QuotaExceededError(f"{method}: quotaExceeded", 403, "quotaExceeded")

    # --- протокол YouTubeClient ---------------------------------------------------

    def channels_list(
        self, *, ids: list[str] | None = None, for_handle: str | None = None
    ) -> dict[str, Any]:
        self._enter("channels.list", {"ids": ids, "for_handle": for_handle})
        if ids is not None:
            if len(ids) > 50:
                raise YouTubeAPIError("too many ids", 400, "badRequest")
            return {"items": [self.channels[i] for i in ids if i in self.channels]}
        handle = (for_handle or "").lower().lstrip("@")
        items = [
            c
            for c in self.channels.values()
            if c.get("snippet", {}).get("customUrl", "").lower().lstrip("@") == handle
        ]
        return {"items": items[:1]}

    def playlist_items_list(
        self, playlist_id: str, *, page_token: str | None = None, max_results: int = 50
    ) -> dict[str, Any]:
        self._enter("playlistItems.list", {"playlist_id": playlist_id, "page_token": page_token})
        if playlist_id not in self.playlists:
            raise YouTubeAPIError("playlist not found", 404, "playlistNotFound")
        items = sorted(
            self.playlists[playlist_id],
            key=lambda i: i["contentDetails"]["videoPublishedAt"],
            reverse=True,
        )
        size = min(max_results, self.page_size)
        start = int(page_token or 0)
        page = items[start : start + size]
        resp: dict[str, Any] = {"items": page}
        if start + size < len(items):
            resp["nextPageToken"] = str(start + size)
        return resp

    def videos_list(self, ids: list[str]) -> dict[str, Any]:
        self._enter("videos.list", {"ids": list(ids)})
        if len(ids) > 50:
            raise YouTubeAPIError("too many ids", 400, "badRequest")
        return {"items": [self.videos[i] for i in ids if i in self.videos]}

    def videos_most_popular(
        self,
        *,
        region_code: str,
        category_id: str | None = None,
        max_results: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        self._enter(
            "videos.mostPopular",
            {"region_code": region_code, "category_id": category_id, "max_results": max_results},
        )
        items = sorted(
            self.videos.values(),
            key=lambda v: int(v.get("statistics", {}).get("viewCount", 0)),
            reverse=True,
        )
        if category_id:
            items = [
                v
                for v in items
                if v.get("snippet", {}).get("categoryId", category_id) == category_id
            ]
        return {"items": items[:max_results]}

    def search_list(
        self,
        *,
        q: str,
        published_after: datetime,
        region_code: str | None = None,
        relevance_language: str | None = None,
        video_duration: str | None = None,
        max_results: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        self._enter(
            "search.list",
            {
                "q": q,
                "published_after": published_after,
                "region_code": region_code,
                "relevance_language": relevance_language,
                "video_duration": video_duration,
            },
        )
        after = ensure_utc(published_after)
        items = [
            i for i in self.search.get(q, []) if parse_dt(i["snippet"]["publishedAt"]) >= after
        ]
        return {"items": items[:max_results]}

    def comment_threads_list(
        self, video_id: str, *, max_results: int = 20, order: str = "relevance"
    ) -> dict[str, Any]:
        self._enter("commentThreads.list", {"video_id": video_id, "max_results": max_results})
        if video_id in self.comments_disabled:
            raise YouTubeAPIError("comments disabled", 403, "commentsDisabled")
        return {"items": self.comments.get(video_id, [])[:max_results]}
