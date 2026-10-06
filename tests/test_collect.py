import sqlite3
from datetime import timedelta

import pytest

from radar.collect.discovery import queries_for_today, run_discovery
from radar.collect.snapshots import collect_snapshots, due_video_ids
from radar.collect.watchlist import add_seed_channels, poll_watchlist
from radar.config import QuotaConfig
from radar.schemas import ChannelStatus, Niche, VideoSnapshot
from radar.youtube.client import YouTube
from radar.youtube.quota import QuotaPlanner

TG = "UCtechguru00000000000000"
SB = "UCsmallbuilder0000000000"
CAND = "UCcandidate1000000000000"


def watch(app, now, *refs):
    for ref in refs:
        ch = app.youtube.resolve_channel(ref, purpose="test", now=now, niche_ids=["ai-tools"])
        app.db.upsert_channel(ch, now, keep_status=False)


def test_seed_channels_resolved_once(app, fake_yt, now):
    r = add_seed_channels(app.youtube, app.db, app.db.list_niches(), now)
    assert r.stats["added"] == 1
    ch = app.db.get_channel(TG)
    assert ch.status == ChannelStatus.WATCHING and ch.niche_ids == ["ai-tools"]
    n_calls = len(fake_yt.calls)
    add_seed_channels(app.youtube, app.db, app.db.list_niches(), now + timedelta(hours=1))
    assert len(fake_yt.calls) == n_calls


def test_hidden_seed_not_restored(app, now):
    add_seed_channels(app.youtube, app.db, app.db.list_niches(), now)
    app.db.set_channel_status(TG, ChannelStatus.HIDDEN)
    add_seed_channels(app.youtube, app.db, app.db.list_niches(), now)
    assert app.db.get_channel(TG).status == ChannelStatus.HIDDEN


def test_poll_watchlist_collects_videos_and_snapshots(app, fake_yt, now):
    watch(app, now, "@techguru", "@smallbuilder")
    r = poll_watchlist(app.youtube, app.db, app.config, now)
    assert r.stats["channels"] == 2
    assert r.stats["new_videos"] == 42 + 15
    assert app.db.count_snapshots() == 57
    assert len(fake_yt.calls_of("playlistItems.list")) == 2
    assert [len(c["ids"]) for c in fake_yt.calls_of("videos.list")] == [50, 7]

    # сразу повторно — не опрашиваем (интервал), квоту не тратим
    before = len(fake_yt.calls)
    r2 = poll_watchlist(app.youtube, app.db, app.config, now + timedelta(minutes=10))
    assert r2.stats["channels"] == 0 and len(fake_yt.calls) == before

    # через час — один вызов на канал, новых видео нет → videos.list не нужен
    r3 = poll_watchlist(app.youtube, app.db, app.config, now + timedelta(minutes=61))
    assert r3.stats["channels"] == 2 and r3.stats["new_videos"] == 0
    assert len(fake_yt.calls_of("videos.list")) == 2


def test_candidate_not_polled(app, fake_yt, now):
    ch = app.youtube.get_channels([CAND], purpose="t", now=now)[0]
    app.db.upsert_channel(ch, now)
    poll_watchlist(app.youtube, app.db, app.config, now)
    assert fake_yt.calls_of("playlistItems.list") == []


def test_snapshot_schedule_and_batching(app, fake_yt, now):
    watch(app, now, "@techguru", "@smallbuilder")
    poll_watchlist(app.youtube, app.db, app.config, now)
    assert due_video_ids(app.db, app.config, now) == []

    t6 = now + timedelta(hours=6)
    fresh = [v.id for v in app.db.list_videos() if t6 - v.published_at < timedelta(days=14)]
    due6 = due_video_ids(app.db, app.config, t6)
    assert sorted(due6) == sorted(fresh)

    t24 = now + timedelta(hours=24)
    due24 = due_video_ids(app.db, app.config, t24)
    tracked = [v.id for v in app.db.list_videos() if t24 - v.published_at < timedelta(days=60)]
    assert sorted(due24) == sorted(tracked)
    assert len(tracked) < 57  # старше 60 дней не собираем

    calls_before = len(fake_yt.calls_of("videos.list"))
    snaps_before = app.db.count_snapshots()
    r = collect_snapshots(app.youtube, app.db, app.config, t24)
    assert r.stats["saved"] == len(tracked)
    assert app.db.count_snapshots() == snaps_before + len(tracked)
    n_batches = len(fake_yt.calls_of("videos.list")) - calls_before
    assert n_batches == -(-len(tracked) // 50)
    # повтор в тот же момент — ничего не собирается
    assert collect_snapshots(app.youtube, app.db, app.config, t24).stats["saved"] == 0


def test_snapshots_append_only(app, now):
    app.db.add_snapshots([VideoSnapshot(video_id="v", views=1, collected_at=now)])
    with pytest.raises(sqlite3.DatabaseError):
        app.db.conn.execute("UPDATE video_snapshots SET views = 2")
    with pytest.raises(sqlite3.DatabaseError):
        app.db.conn.execute("DELETE FROM video_snapshots")
    assert app.db.snapshots_for("v")[0].views == 1


def test_discovery_finds_candidates_in_range(app, fake_yt, now):
    add_seed_channels(app.youtube, app.db, app.db.list_niches(), now)  # techguru known
    r = run_discovery(app.youtube, app.db, app.config, app.db.list_niches(), now)
    assert r.stats["searches"] == 2
    cands = {c.id for c in app.db.list_channels(status=ChannelStatus.CANDIDATE)}
    assert cands == {CAND, SB}  # bigmedia > max_subs, hiddensubs без подписчиков
    assert r.stats["rejected"] == 2
    call = fake_yt.calls_of("search.list")[0]
    assert call["region_code"] == "RU" and call["relevance_language"] == "ru"
    assert call["published_after"] == now - timedelta(days=30)

    # в те же сутки лимит discovery_per_day исчерпан
    r2 = run_discovery(
        app.youtube, app.db, app.config, app.db.list_niches(), now + timedelta(minutes=30)
    )
    assert r2.stats["searches"] == 0
    # на следующие сутки — снова
    r3 = run_discovery(
        app.youtube, app.db, app.config, app.db.list_niches(), now + timedelta(days=1)
    )
    assert r3.stats["searches"] == 2


def test_discovery_auto_approve(app, now):
    app.config.discovery.auto_approve = True
    run_discovery(app.youtube, app.db, app.config, app.db.list_niches(), now)
    assert app.db.get_channel(CAND).status == ChannelStatus.WATCHING


def test_discovery_respects_budget(db, fake_yt, app, now):
    planner = QuotaPlanner(
        db, QuotaConfig(daily_budget=250, discovery_reserve=0, safety_margin=100)
    )
    yt = YouTube(fake_yt, planner, app.config.formats)
    r = run_discovery(yt, db, app.config, db.list_niches(), now)
    assert r.deferred and r.stats["searches"] == 1
    assert planner.used(now) <= 250 - 100 + 1  # search не залез в safety_margin
    # найденное первым search сохранено
    assert db.list_channels(status=ChannelStatus.CANDIDATE)


def test_query_rotation(now):
    n = Niche(id="x", name="x", seed_queries=["a", "b", "c"], discovery_per_day=1)
    days = {queries_for_today(n, 0, now + timedelta(days=d))[0] for d in range(3)}
    assert days == {"a", "b", "c"}
    assert queries_for_today(n, 1, now) == []


def test_phase1_acceptance(app, fake_yt, now):
    """Каналы, видео и снимки собираются на фикстурах; бюджет квоты соблюдён и учтён полностью."""
    niches = app.db.list_niches()
    add_seed_channels(app.youtube, app.db, niches, now)
    run_discovery(app.youtube, app.db, app.config, niches, now)
    app.db.set_channel_status(SB, ChannelStatus.WATCHING)
    app.db.set_channel_status(CAND, ChannelStatus.WATCHING)
    poll_watchlist(app.youtube, app.db, app.config, now)
    collect_snapshots(app.youtube, app.db, app.config, now + timedelta(hours=7))

    assert len(app.db.list_channels(status=ChannelStatus.WATCHING)) == 3
    assert len(app.db.list_videos()) == 42 + 15 + 5
    assert app.db.count_snapshots() > len(app.db.list_videos())
    total = app.db.conn.execute("SELECT SUM(units) FROM quota_ledger").fetchone()[0]
    assert total == app.db.count_quota_entries() + 99 * len(fake_yt.calls_of("search.list"))
    assert app.db.count_quota_entries() == len(fake_yt.calls)
    assert app.planner.used(now) <= app.config.quota.daily_budget


def test_discovery_links_known_channel_to_new_niche(app, now):
    from radar.schemas import Niche

    add_seed_channels(app.youtube, app.db, app.db.list_niches(), now)  # techguru в ai-tools
    other = Niche(
        id="other", name="Другая", seed_queries=["нейросети для работы"], discovery_per_day=1
    )
    app.db.upsert_niche(other, now)
    r = run_discovery(app.youtube, app.db, app.config, [other], now)
    assert r.stats["linked"] >= 1
    assert app.db.get_channel(TG).niche_ids == ["ai-tools", "other"]
