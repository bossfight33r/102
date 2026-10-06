"""Весь конвейер через настоящий HttpYouTubeClient: MockTransport отвечает данными фикстур.

Проверяет реальные пути и параметры запросов YouTube Data API v3 (part, id, forHandle, playlistId, …),
которые FakeYouTubeClient не видит.
"""

from datetime import timedelta

import httpx
import pytest

from radar.app import App
from radar.tick import run_tick
from radar.timeutil import parse_dt
from radar.youtube.api import HttpYouTubeClient
from radar.youtube.client import YouTubeAPIError
from radar.youtube.fake import FakeYouTubeClient

KEY = "AIzaSyINTEGRATIONTEST0000000000000000"
EXPECTED_PART = {
    "/youtube/v3/channels": "snippet,statistics,contentDetails",
    "/youtube/v3/playlistItems": "contentDetails,snippet",
    "/youtube/v3/videos": "snippet,contentDetails,statistics",
    "/youtube/v3/search": "snippet",
    "/youtube/v3/commentThreads": "snippet",
}


def make_transport(fake: FakeYouTubeClient, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        p = req.url.params
        assert p["key"] == KEY
        assert req.url.host == "www.googleapis.com"
        assert p["part"] == EXPECTED_PART[req.url.path], req.url.path
        try:
            match req.url.path:
                case "/youtube/v3/channels":
                    body = (
                        fake.channels_list(ids=p["id"].split(","))
                        if "id" in p
                        else fake.channels_list(for_handle=p["forHandle"])
                    )
                case "/youtube/v3/playlistItems":
                    body = fake.playlist_items_list(
                        p["playlistId"],
                        page_token=p.get("pageToken"),
                        max_results=int(p["maxResults"]),
                    )
                case "/youtube/v3/videos":
                    body = fake.videos_list(p["id"].split(","))
                case "/youtube/v3/search":
                    assert p["type"] == "video" and p["order"] == "viewCount"
                    body = fake.search_list(
                        q=p["q"],
                        published_after=parse_dt(p["publishedAfter"]),
                        region_code=p.get("regionCode"),
                        relevance_language=p.get("relevanceLanguage"),
                        max_results=int(p["maxResults"]),
                    )
                case "/youtube/v3/commentThreads":
                    assert p["order"] == "relevance"
                    body = fake.comment_threads_list(p["videoId"], max_results=int(p["maxResults"]))
                case _:
                    return httpx.Response(404)
        except YouTubeAPIError as e:
            return httpx.Response(
                e.status or 400,
                json={
                    "error": {"code": e.status, "message": str(e), "errors": [{"reason": e.reason}]}
                },
            )
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


@pytest.fixture
def http_app(settings, db, fake_yt, fake_llm, notifier, now):
    seen: list[httpx.Request] = []
    client = HttpYouTubeClient(
        KEY, http=httpx.Client(transport=make_transport(fake_yt, seen)), sleep=lambda s: None
    )
    a = App.build(settings, db=db, youtube_client=client, llm=fake_llm, notifier=notifier)
    a.sync_niches(now)
    return a, seen


def test_tick_through_real_http_client(http_app, notifier, now):
    app, seen = http_app
    results = {r.name: r for r in run_tick(app, now)}
    assert results["watchlist"].stats["new_videos"] == 42
    assert results["discovery"].stats["searches"] == 2
    assert results["score"].stats["outliers"] == 2
    assert results["digest"].stats["sent"] == 3
    # каждый HTTP-запрос учтён в журнале квоты, search стоит 100
    assert app.db.count_quota_entries() == len(seen)
    searches = [r for r in seen if r.url.path.endswith("/search")]
    assert app.planner.used(now) == len(seen) + 99 * len(searches)
    assert {r.url.params["regionCode"] for r in searches} == {"RU"}
    assert any(r.url.params.get("forHandle") == "@techguru" for r in seen)
    # videos.list — не больше 50 id за запрос
    assert all(
        len(r.url.params["id"].split(",")) <= 50 for r in seen if r.url.path.endswith("/videos")
    )


def test_quota_exceeded_over_http(http_app, fake_yt, now):
    app, seen = http_app
    fake_yt.quota_exceeded = True

    # FakeYouTubeClient кидает QuotaExceededError → транспорт отдаёт 403 quotaExceeded как настоящий API
    run_tick(app, now)
    assert app.planner.paused_until(now) is not None
    n = len(seen)
    run_tick(app, now + timedelta(minutes=15))
    assert len(seen) == n  # на паузе HTTP-запросов нет
