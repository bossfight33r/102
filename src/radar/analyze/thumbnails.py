"""Превью для анализа: скачивание по URL из API с кешем в data/cache/thumbs. Только *.ytimg.com."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from radar.log import get_logger
from radar.schemas import ImageInput, Video

log = get_logger(__name__)

ALLOWED_HOST_SUFFIX = ".ytimg.com"
MAX_BYTES = 2_000_000


def _http_fetch(url: str) -> bytes:
    resp = httpx.get(url, timeout=15.0, follow_redirects=False)
    resp.raise_for_status()
    return resp.content


def _media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class ThumbnailStore:
    def __init__(self, cache_dir: Path, fetch: Callable[[str], bytes] = _http_fetch) -> None:
        self.cache_dir = cache_dir
        self.fetch = fetch

    def candidates(self, video: Video) -> list[str]:
        urls = [video.thumbnail_url] if video.thumbnail_url else []
        urls.append(f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg")
        return [u for u in dict.fromkeys(urls) if u]

    def get(self, video: Video) -> ImageInput | None:
        for url in self.candidates(video):
            host = urlparse(url).hostname or ""
            if not (host == ALLOWED_HOST_SUFFIX[1:] or host.endswith(ALLOWED_HOST_SUFFIX)):
                log.warning("thumbnail_host_rejected", host=host)
                continue
            path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".img")
            if path.exists():
                data = path.read_bytes()
                return ImageInput(media_type=_media_type(data), data=data)
            try:
                data = self.fetch(url)
            except Exception as e:  # сеть/404 — анализ продолжится без картинки
                log.info("thumbnail_fetch_failed", video_id=video.id, error=type(e).__name__)
                continue
            if not data or len(data) > MAX_BYTES:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return ImageInput(media_type=_media_type(data), data=data)
        return None
