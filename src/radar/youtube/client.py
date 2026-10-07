"""Протокол сырого YouTube-клиента и обёртка YouTube, через которую проходит каждый вызов с учётом квоты."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, Protocol

from radar.config import FormatConfig
from radar.log import get_logger
from radar.schemas import (
    Channel,
    ChannelStatus,
    Chapter,
    Comment,
    SearchHit,
    Video,
    VideoFormat,
    VideoStats,
)
from radar.timeutil import iso, parse_dt
from radar.youtube.quota import QuotaPlanner

log = get_logger(__name__)

BATCH_SIZE = 50


class YouTubeAPIError(Exception):
    def __init__(self, message: str, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class QuotaExceededError(YouTubeAPIError):
    """API ответил quotaExceeded / dailyLimitExceeded."""


class YouTubeClient(Protocol):
    """Сырые REST-методы YouTube Data API v3. Возвращают JSON-ответ как dict."""

    def channels_list(
        self, *, ids: list[str] | None = None, for_handle: str | None = None
    ) -> dict[str, Any]: ...

    def playlist_items_list(
        self, playlist_id: str, *, page_token: str | None = None, max_results: int = 50
    ) -> dict[str, Any]: ...

    def videos_list(self, ids: list[str]) -> dict[str, Any]: ...

    def videos_most_popular(
        self,
        *,
        region_code: str,
        category_id: str | None = None,
        max_results: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    def comment_threads_list(
        self, video_id: str, *, max_results: int = 20, order: str = "relevance"
    ) -> dict[str, Any]: ...


# --- парсеры ---------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)
_CHAPTER_RE = re.compile(r"^\s*[\(\[]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\)\]]?\s*[-–—:|]?\s*(.+?)\s*$")


def parse_duration(value: str | None) -> int:
    """ISO 8601 (PT1H2M3S) -> секунды. Неизвестный формат -> 0."""
    if not value:
        return 0
    m = _DURATION_RE.match(value)
    if not m:
        return 0
    d, h, mi, s = (int(m.group(k) or 0) for k in ("d", "h", "m", "s"))
    return d * 86400 + h * 3600 + mi * 60 + s


def _ts_to_sec(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def parse_chapters(description: str) -> list[Chapter]:
    """Главы по правилам YouTube: первая с 0:00, минимум 3, по возрастанию."""
    chapters: list[Chapter] = []
    for line in description.splitlines():
        m = _CHAPTER_RE.match(line)
        if m:
            chapters.append(Chapter(start_sec=_ts_to_sec(m.group(1)), title=m.group(2)[:200]))
    if len(chapters) < 3 or chapters[0].start_sec != 0:
        return []
    starts = [c.start_sec for c in chapters]
    if starts != sorted(starts) or len(set(starts)) != len(starts):
        return []
    return chapters


def classify_format(duration_sec: int, cfg: FormatConfig) -> VideoFormat:
    return VideoFormat.SHORT if 0 < duration_sec <= cfg.short_max_sec else VideoFormat.LONG


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _best_thumbnail(thumbs: dict[str, Any]) -> str | None:
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbs and thumbs[key].get("url"):
            return thumbs[key]["url"]
    return None


def parse_channel(
    item: dict[str, Any], now: datetime, niche_ids: Iterable[str], status: ChannelStatus
) -> Channel | None:
    uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads:
        return None
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    subs = (
        None if stats.get("hiddenSubscriberCount") else _int_or_none(stats.get("subscriberCount"))
    )
    return Channel(
        id=item["id"],
        title=snippet.get("title", item["id"]),
        handle=snippet.get("customUrl"),
        subs=subs,
        uploads_playlist_id=uploads,
        niche_ids=sorted(set(niche_ids)),
        status=status,
        added_at=now,
    )


def parse_video(item: dict[str, Any], cfg: FormatConfig) -> tuple[Video, VideoStats]:
    snippet = item.get("snippet", {})
    duration = parse_duration(item.get("contentDetails", {}).get("duration"))
    description = snippet.get("description", "") or ""
    video = Video(
        id=item["id"],
        channel_id=snippet["channelId"],
        title=snippet.get("title", ""),
        description=description,
        tags=snippet.get("tags", []) or [],
        duration_sec=duration,
        format=classify_format(duration, cfg),
        published_at=parse_dt(snippet["publishedAt"]),
        thumbnail_url=_best_thumbnail(snippet.get("thumbnails", {})),
        chapters=parse_chapters(description),
    )
    st = item.get("statistics", {})
    stats = VideoStats(
        video_id=item["id"],
        views=_int_or_none(st.get("viewCount")) or 0,
        likes=_int_or_none(st.get("likeCount")),
        comments=_int_or_none(st.get("commentCount")),
    )
    return video, stats


def chunked[T](items: list[T], size: int = BATCH_SIZE) -> list[list[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# --- обёртка с учётом квоты -----------------------------------------------------


class YouTube:
    """Единственная точка доступа к YouTube API для бизнес-логики.

    Каждый вызов: QuotaPlanner.check -> вызов -> запись в quota_ledger.
    QuotaDeferred / QuotaExceededError пробрасываются вызывающему коду (задача откладывается).
    """

    def __init__(self, client: YouTubeClient, planner: QuotaPlanner, formats: FormatConfig) -> None:
        self.client = client
        self.planner = planner
        self.formats = formats

    def _call(
        self, method: str, purpose: str, now: datetime, fn: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        self.planner.check(method, purpose, now)
        try:
            result = fn()
        except QuotaExceededError:
            self.planner.record(method, purpose, now)
            self.planner.pause_until_reset(now)
            raise
        except YouTubeAPIError as e:
            self.planner.record(method, purpose, now)
            log.warning("youtube_api_error", method=method, status=e.status, reason=e.reason)
            raise
        self.planner.record(method, purpose, now)
        return result

    def get_channels(
        self,
        ids: list[str],
        *,
        purpose: str,
        now: datetime,
        niche_ids: Iterable[str] = (),
        status: ChannelStatus = ChannelStatus.CANDIDATE,
    ) -> list[Channel]:
        niche_ids = list(niche_ids)
        out: list[Channel] = []
        for batch in chunked(sorted(set(ids))):
            resp = self._call(
                "channels.list", purpose, now, lambda b=batch: self.client.channels_list(ids=b)
            )
            for item in resp.get("items", []):
                ch = parse_channel(item, now, niche_ids, status)
                if ch:
                    out.append(ch)
        return out

    def resolve_channel(
        self,
        ref: str,
        *,
        purpose: str,
        now: datetime,
        niche_ids: Iterable[str] = (),
        status: ChannelStatus = ChannelStatus.WATCHING,
    ) -> Channel | None:
        """ref: id канала (UC...), @handle или ссылка youtube.com/@handle | /channel/UC..."""
        ref = ref.strip().rstrip("/")
        m = re.search(r"/channel/(UC[\w-]{22})", ref)
        if m:
            ref = m.group(1)
        elif "/@" in ref:
            ref = "@" + ref.split("/@", 1)[1].split("/")[0]
        if re.fullmatch(r"UC[\w-]{22}", ref):
            found = self.get_channels(
                [ref], purpose=purpose, now=now, niche_ids=niche_ids, status=status
            )
            return found[0] if found else None
        handle = ref if ref.startswith("@") else "@" + ref
        resp = self._call(
            "channels.list", purpose, now, lambda: self.client.channels_list(for_handle=handle)
        )
        items = resp.get("items", [])
        return parse_channel(items[0], now, niche_ids, status) if items else None

    def list_uploads(
        self,
        playlist_id: str,
        *,
        purpose: str,
        now: datetime,
        max_items: int = 50,
        stop_before: datetime | None = None,
    ) -> list[tuple[str, datetime | None]]:
        """(video_id, published_at) из uploads-плейлиста, новые сверху.

        Листает страницы, пока не набрано max_items или не встречено видео старше stop_before.
        """
        out: list[tuple[str, datetime | None]] = []
        token: str | None = None
        while len(out) < max_items:
            resp = self._call(
                "playlistItems.list",
                purpose,
                now,
                lambda t=token: self.client.playlist_items_list(
                    playlist_id, page_token=t, max_results=50
                ),
            )
            reached_old = False
            for item in resp.get("items", []):
                cd = item.get("contentDetails", {})
                vid = cd.get("videoId") or item.get("snippet", {}).get("resourceId", {}).get(
                    "videoId"
                )
                if not vid:
                    continue
                pub_raw = cd.get("videoPublishedAt") or item.get("snippet", {}).get("publishedAt")
                pub = parse_dt(pub_raw) if pub_raw else None
                if stop_before and pub and pub < stop_before:
                    reached_old = True
                    break
                out.append((vid, pub))
            token = resp.get("nextPageToken")
            if reached_old or not token:
                break
        return out[:max_items]

    def get_videos(
        self, ids: list[str], *, purpose: str, now: datetime
    ) -> list[tuple[Video, VideoStats]]:
        out: list[tuple[Video, VideoStats]] = []
        for batch in chunked(list(dict.fromkeys(ids))):
            resp = self._call(
                "videos.list", purpose, now, lambda b=batch: self.client.videos_list(b)
            )
            for item in resp.get("items", []):
                if "snippet" not in item:
                    continue
                out.append(parse_video(item, self.formats))
        return out

    def most_popular(
        self,
        *,
        purpose: str,
        now: datetime,
        region_code: str,
        category_id: str | None = None,
        max_results: int = 50,
    ) -> list[tuple[Video, VideoStats]]:
        """Официальный топ YouTube (videos.list chart=mostPopular): 1 ед. за 50 видео."""
        resp = self._call(
            "videos.list",
            purpose,
            now,
            lambda: self.client.videos_most_popular(
                region_code=region_code, category_id=category_id, max_results=min(max_results, 50)
            ),
        )
        return [
            parse_video(item, self.formats) for item in resp.get("items", []) if "snippet" in item
        ]

    def search(
        self,
        q: str,
        *,
        purpose: str,
        now: datetime,
        published_after: datetime,
        region_code: str | None,
        relevance_language: str | None,
        video_duration: str | None = None,
        max_results: int = 50,
    ) -> list[SearchHit]:
        resp = self._call(
            "search.list",
            purpose,
            now,
            lambda: self.client.search_list(
                q=q,
                published_after=published_after,
                region_code=region_code,
                relevance_language=relevance_language,
                video_duration=video_duration,
                max_results=max_results,
            ),
        )
        hits: list[SearchHit] = []
        for item in resp.get("items", []):
            vid = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {})
            if not vid or not snippet.get("channelId"):
                continue
            pub = snippet.get("publishedAt")
            hits.append(
                SearchHit(
                    video_id=vid,
                    channel_id=snippet["channelId"],
                    published_at=parse_dt(pub) if pub else None,
                    title=snippet.get("title", ""),
                )
            )
        log.info("search_done", q=q, hits=len(hits), published_after=iso(published_after))
        return hits

    def top_comments(self, video_id: str, n: int, *, purpose: str, now: datetime) -> list[Comment]:
        """Топ комментариев по релевантности. Отключённые комментарии -> пустой список."""
        try:
            resp = self._call(
                "commentThreads.list",
                purpose,
                now,
                lambda: self.client.comment_threads_list(video_id, max_results=min(n, 100)),
            )
        except QuotaExceededError:
            raise
        except YouTubeAPIError as e:
            if e.reason in {"commentsDisabled", "videoNotFound", "forbidden"}:
                return []
            raise
        out: list[Comment] = []
        for item in resp.get("items", []):
            top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            text = top.get("textOriginal") or top.get("textDisplay") or ""
            if text.strip():
                out.append(
                    Comment(text=text.strip()[:1000], likes=_int_or_none(top.get("likeCount")) or 0)
                )
        return out[:n]
