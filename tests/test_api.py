"""HttpYouTubeClient на httpx.MockTransport — без сети."""

import json
import logging

import httpx
import pytest

from radar.log import configure_logging
from radar.youtube.api import HttpYouTubeClient
from radar.youtube.client import QuotaExceededError, YouTubeAPIError

KEY = "AIzaSyTESTKEY000000000000000000000000"


def make(handler, **kw):
    sleeps: list[float] = []
    client = HttpYouTubeClient(
        KEY, http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, **kw
    )
    return client, sleeps


def err(status, reason):
    return httpx.Response(
        status, json={"error": {"code": status, "message": "m", "errors": [{"reason": reason}]}}
    )


def test_retry_then_success():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"items": []})

    client, sleeps = make(handler)
    assert client.videos_list(["a"]) == {"items": []}
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]
    assert calls[0].url.params["key"] == KEY
    assert calls[0].url.path == "/youtube/v3/videos"


def test_quota_exceeded_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        return err(403, "quotaExceeded")

    client, sleeps = make(handler)
    with pytest.raises(QuotaExceededError):
        client.search_list(q="x", published_after=__import__("datetime").datetime(2026, 1, 1))
    assert len(calls) == 1 and sleeps == []


def test_retries_exhausted():
    client, sleeps = make(lambda req: err(500, "backendError"), max_retries=2)
    with pytest.raises(YouTubeAPIError):
        client.channels_list(ids=["x"])
    assert len(sleeps) == 2


def test_network_error_retried():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": 1})

    client, _ = make(handler)
    assert client.playlist_items_list("UU1") == {"ok": 1}


def test_4xx_raises_with_reason():
    client, _ = make(lambda req: err(403, "commentsDisabled"))
    with pytest.raises(YouTubeAPIError) as e:
        client.comment_threads_list("v")
    assert e.value.reason == "commentsDisabled"
    assert KEY not in str(e.value)


def test_videos_list_limit():
    client, _ = make(lambda req: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        client.videos_list([str(i) for i in range(51)])


def test_key_not_logged(capsys):
    configure_logging("DEBUG", json=True, secrets=[KEY])
    logging.getLogger("httpx").info("GET https://x/videos?key=%s", KEY)
    client, _ = make(lambda req: httpx.Response(500), max_retries=1)
    with pytest.raises(YouTubeAPIError):
        client.videos_list(["a"])
    out = capsys.readouterr()
    assert KEY not in out.err + out.out
    assert json  # json renderer используется
