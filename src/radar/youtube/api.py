"""Реальный клиент YouTube Data API v3: прямые REST-запросы через httpx, только API-ключ."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from radar.log import get_logger
from radar.timeutil import ensure_utc
from radar.youtube.client import QuotaExceededError, YouTubeAPIError

log = get_logger(__name__)

BASE_URL = "https://www.googleapis.com/youtube/v3"
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


class HttpYouTubeClient:
    """Ретраи с экспоненциальной паузой на сетевые ошибки, 5xx и rate limit.

    quotaExceeded не ретраится — сразу QuotaExceededError.
    Ключ передаётся в query и никогда не логируется (логируется только путь).
    """

    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("YOUTUBE_API_KEY не задан")
        self._key = api_key
        self._http = http or httpx.Client(timeout=20.0)
        self._max_retries = max_retries
        self._backoff = backoff_base
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {k: v for k, v in params.items() if v is not None}
        query["key"] = self._key
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http.get(BASE_URL + path, params=query)
            except httpx.TransportError as e:
                last_error = YouTubeAPIError(f"network error on {path}: {type(e).__name__}")
                self._pause(attempt, path, "network")
                continue
            if resp.status_code == 200:
                return resp.json()
            reason, message = _error_reason(resp)
            if reason in _QUOTA_REASONS:
                raise QuotaExceededError(f"{path}: {reason}", resp.status_code, reason)
            if resp.status_code >= 500 or resp.status_code == 429 or reason in _RETRY_REASONS:
                last_error = YouTubeAPIError(
                    f"{path}: {resp.status_code} {reason}", resp.status_code, reason
                )
                self._pause(attempt, path, reason or str(resp.status_code))
                continue
            raise YouTubeAPIError(
                f"{path}: {resp.status_code} {reason} {message}", resp.status_code, reason
            )
        assert last_error is not None
        raise last_error

    def _pause(self, attempt: int, path: str, why: str) -> None:
        if attempt >= self._max_retries:
            return
        delay = self._backoff * (2**attempt) + random.uniform(0, self._backoff / 2)
        log.info("youtube_retry", path=path, attempt=attempt + 1, delay=round(delay, 2), why=why)
        self._sleep(delay)

    def channels_list(
        self, *, ids: list[str] | None = None, for_handle: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"part": "snippet,statistics,contentDetails", "maxResults": 50}
        if ids:
            params["id"] = ",".join(ids)
        elif for_handle:
            params["forHandle"] = for_handle
        else:
            raise ValueError("нужен ids или for_handle")
        return self._get("/channels", params)

    def playlist_items_list(
        self, playlist_id: str, *, page_token: str | None = None, max_results: int = 50
    ) -> dict[str, Any]:
        return self._get(
            "/playlistItems",
            {
                "part": "contentDetails,snippet",
                "playlistId": playlist_id,
                "maxResults": max_results,
                "pageToken": page_token,
            },
        )

    def videos_list(self, ids: list[str]) -> dict[str, Any]:
        if len(ids) > 50:
            raise ValueError("videos.list принимает не более 50 id")
        return self._get(
            "/videos",
            {"part": "snippet,contentDetails,statistics", "id": ",".join(ids), "maxResults": 50},
        )

    def videos_most_popular(
        self,
        *,
        region_code: str,
        category_id: str | None = None,
        max_results: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/videos",
            {
                "part": "snippet,contentDetails,statistics",
                "chart": "mostPopular",
                "regionCode": region_code,
                "videoCategoryId": category_id,
                "maxResults": max_results,
                "pageToken": page_token,
            },
        )

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
        return self._get(
            "/search",
            {
                "part": "snippet",
                "type": "video",
                "q": q,
                "order": "viewCount",
                "publishedAfter": ensure_utc(published_after).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "regionCode": region_code,
                "relevanceLanguage": relevance_language,
                "videoDuration": video_duration,
                "maxResults": max_results,
                "pageToken": page_token,
            },
        )

    def comment_threads_list(
        self, video_id: str, *, max_results: int = 20, order: str = "relevance"
    ) -> dict[str, Any]:
        return self._get(
            "/commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": max_results,
                "order": order,
                "textFormat": "plainText",
            },
        )


def _error_reason(resp: httpx.Response) -> tuple[str, str]:
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return "", ""
    errors = err.get("errors") or [{}]
    return errors[0].get("reason", ""), str(err.get("message", ""))[:200]


def fetch_thumbnail(url: str) -> bytes:
    """Скачать превью с CDN YouTube (i.ytimg.com). Не вызов Data API — квоту не тратит."""
    resp = httpx.get(url, timeout=15.0, follow_redirects=False)
    resp.raise_for_status()
    return resp.content
