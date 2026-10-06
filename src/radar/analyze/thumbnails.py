"""Превью для анализа: скачивание по URL из API с кешем в data/cache/thumbs. Только *.ytimg.com."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from radar.log import get_logger
from radar.schemas import ImageInput, Video
from radar.youtube.api import fetch_thumbnail

log = get_logger(__name__)

ALLOWED_HOST_SUFFIX = ".ytimg.com"
MAX_BYTES = 2_000_000


def _media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class ThumbnailStore:
    def __init__(self, cache_dir: Path, fetch: Callable[[str], bytes] = fetch_thumbnail) -> None:
        self.cache_dir = cache_dir
        self.fetch = fetch

    def candidates(self, video: Video) -> list[str]:
        # Для LLM — hqdefault 480×360 (~230 токенов) вместо maxres 1280×720 (~1200): деталей хватает.
        urls = [f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg"]
        if video.thumbnail_url:
            urls.append(video.thumbnail_url)
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
