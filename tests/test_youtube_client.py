from radar.config import FormatConfig, QuotaConfig
from radar.schemas import VideoFormat
from radar.youtube.client import YouTube, parse_chapters, parse_duration, parse_video
from radar.youtube.quota import QuotaPlanner


def test_parse_duration():
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("PT45S") == 45
    assert parse_duration("P1DT1S") == 86401
    assert parse_duration("P0D") == 0
    assert parse_duration("garbage") == 0
    assert parse_duration(None) == 0


def test_parse_chapters():
    desc = "Интро\n0:00 Старт\n1:05 Шаг 1\n10:30 - Шаг 2\n1:02:03 Финал"
    ch = parse_chapters(desc)
    assert [c.start_sec for c in ch] == [0, 65, 630, 3723]
    assert ch[2].title == "Шаг 2"
    assert parse_chapters("0:00 a\n1:00 b") == []  # меньше 3
    assert parse_chapters("0:10 a\n1:00 b\n2:00 c") == []  # не с нуля


def test_parse_video_formats():
    item = {
        "id": "v1",
        "snippet": {"publishedAt": "2026-10-01T00:00:00Z", "channelId": "c", "title": "t"},
        "contentDetails": {"duration": "PT59S"},
        "statistics": {"viewCount": "10"},
    }
    v, s = parse_video(item, FormatConfig())
    assert v.format == VideoFormat.SHORT and s.views == 10 and s.likes is None
    item["contentDetails"]["duration"] = "PT3M1S"
    assert parse_video(item, FormatConfig())[0].format == VideoFormat.LONG


def _yt(db, fake_yt):
    return YouTube(fake_yt, QuotaPlanner(db, QuotaConfig()), FormatConfig())


def test_videos_batched_by_50(db, fake_yt, now):
    ids = list(fake_yt.videos)[:59]
    ids = (ids * 3)[:120]  # дубли схлопываются
    res = _yt(db, fake_yt).get_videos(ids, purpose="t", now=now)
    calls = fake_yt.calls_of("videos.list")
    assert len(res) == 59
    assert [len(c["ids"]) for c in calls] == [50, 9]


def test_resolve_channel_variants(db, fake_yt, now):
    yt = _yt(db, fake_yt)
    for ref in (
        "@techguru",
        "techguru",
        "https://www.youtube.com/@techguru/videos",
        "UCtechguru00000000000000",
        "https://youtube.com/channel/UCtechguru00000000000000",
    ):
        ch = yt.resolve_channel(ref, purpose="t", now=now)
        assert ch is not None and ch.id == "UCtechguru00000000000000", ref
    assert yt.resolve_channel("@nobody", purpose="t", now=now) is None


def test_hidden_subs_is_none(db, fake_yt, now):
    chans = _yt(db, fake_yt).get_channels(["UChiddensubs000000000000"], purpose="t", now=now)
    assert chans[0].subs is None


def test_list_uploads_paginates_and_stops(db, fake_yt, now):
    from datetime import timedelta

    fake_yt.page_size = 10
    yt = _yt(db, fake_yt)
    items = yt.list_uploads("UUtechguru00000000000000", purpose="t", now=now, max_items=100)
    assert len(items) == 42
    recent = yt.list_uploads(
        "UUtechguru00000000000000",
        purpose="t",
        now=now,
        max_items=100,
        stop_before=now - timedelta(days=7),
    )
    assert all(p >= now - timedelta(days=7) for _, p in recent)


def test_comments_disabled_returns_empty(db, fake_yt, now):
    fake_yt.comments_disabled.add("tgLONGOUT01")
    assert _yt(db, fake_yt).top_comments("tgLONGOUT01", 20, purpose="t", now=now) == []
