from datetime import UTC, datetime, timedelta

import pytest

from radar.config import FormatConfig, QuotaConfig
from radar.timeutil import next_quota_reset, quota_day
from radar.youtube.client import QuotaExceededError, YouTube
from radar.youtube.quota import QuotaDeferred, QuotaPlanner


def planner(db, **kw):
    return QuotaPlanner(db, QuotaConfig(**kw))


def test_record_and_used(db, now):
    p = planner(db)
    p.record("search.list", "discovery:x", now)
    p.record("videos.list", "snapshots", now)
    assert p.used(now) == 101
    assert p.discovery_used(now) == 100


def test_cheap_calls_do_not_eat_discovery_reserve(db, now):
    p = planner(db, daily_budget=100, discovery_reserve=30, safety_margin=0)
    for _ in range(70):
        p.check("videos.list", "snapshots", now)
        p.record("videos.list", "snapshots", now)
    with pytest.raises(QuotaDeferred):
        p.check("videos.list", "snapshots", now)
    # discovery-цели всё ещё могут тратить резерв
    p.check("channels.list", "discovery:x", now)


def test_expensive_calls_deferred_near_limit(db, now):
    p = planner(db, daily_budget=1000, discovery_reserve=0, safety_margin=150)
    for _ in range(8):
        p.check("search.list", "discovery:x", now)
        p.record("search.list", "discovery:x", now)
    assert p.used(now) == 800
    with pytest.raises(QuotaDeferred):
        p.check("search.list", "discovery:x", now)  # 800+100 > 1000-150
    p.check("videos.list", "snapshots", now)  # дешёвое ещё можно
    assert p.calls_available("search.list", "discovery:x", now) == 0


def test_unknown_method_cost(db):
    with pytest.raises(KeyError):
        planner(db).cost("activities.list")


def test_quota_day_is_pacific():
    # 06:00 UTC 6 октября = 23:00 PDT 5 октября
    assert quota_day(datetime(2026, 10, 6, 6, 0, tzinfo=UTC)).isoformat() == "2026-10-05"
    assert quota_day(datetime(2026, 10, 6, 8, 0, tzinfo=UTC)).isoformat() == "2026-10-06"
    assert next_quota_reset(datetime(2026, 10, 6, 6, 0, tzinfo=UTC)) == datetime(
        2026, 10, 6, 7, 0, tzinfo=UTC
    )


def test_quota_exceeded_pauses_and_alerts(db, fake_yt, now):
    p = planner(db)
    yt = YouTube(fake_yt, p, FormatConfig())
    fake_yt.quota_exceeded = True
    with pytest.raises(QuotaExceededError):
        yt.get_videos(["tgLONGOUT01"], purpose="snapshots", now=now)
    assert p.paused_until(now) == next_quota_reset(now)
    alerts = db.pending_alerts()
    assert len(alerts) == 1 and "quotaExceeded" in alerts[0][1]
    # пока пауза — вызовы не доходят до API
    fake_yt.quota_exceeded = False
    calls_before = len(fake_yt.calls)
    with pytest.raises(QuotaDeferred):
        yt.get_videos(["tgLONGOUT01"], purpose="snapshots", now=now + timedelta(minutes=5))
    assert len(fake_yt.calls) == calls_before
    # после сброса — снова можно
    after = next_quota_reset(now) + timedelta(minutes=1)
    assert yt.get_videos(["tgLONGOUT01"], purpose="snapshots", now=after)
    # повторный quotaExceeded в те же сутки не плодит алерты
    p.pause_until_reset(now)
    assert len(db.pending_alerts()) == 1


def test_every_api_call_is_metered(db, fake_yt, now):
    p = planner(db)
    yt = YouTube(fake_yt, p, FormatConfig())
    yt.get_videos([v for v in fake_yt.videos][:120], purpose="t", now=now)
    yt.get_channels(list(fake_yt.channels), purpose="t", now=now)
    yt.resolve_channel("@techguru", purpose="t", now=now)
    yt.list_uploads("UUtechguru00000000000000", purpose="t", now=now)
    yt.top_comments("tgLONGOUT01", 20, purpose="t", now=now)
    assert db.count_quota_entries() == len(fake_yt.calls)
    assert p.used(now) == len(fake_yt.calls)
