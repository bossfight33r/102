import math
from datetime import timedelta

import pytest

from radar.config import AppConfig, ScoringConfig
from radar.schemas import ChannelBaseline, VideoFormat, VideoSnapshot
from radar.score.baseline import (
    MAD_SCALE,
    Sample,
    compute_baseline,
    mad,
    median,
    pick_samples,
    robust_z,
)
from radar.score.outliers import combine_score, score_all, views_at_age
from synth import add_video, growth, mature_channel

CFG = ScoringConfig()


def test_median_mad():
    assert median([1, 2, 3, 4, 100]) == 3
    assert mad([1, 2, 3, 4, 100]) == 1
    assert median([1, 2, 3, 4]) == 2.5


def test_robust_z_and_floor(now):
    b = ChannelBaseline(
        channel_id="c",
        format=VideoFormat.LONG,
        median_views=1000,
        mad=0.5,
        sample_size=10,
        reliable=True,
        computed_at=now,
    )
    assert robust_z(1000 * math.e, b, CFG) == pytest.approx(1 / (MAD_SCALE * 0.5))
    flat = b.model_copy(update={"mad": 0.0})  # все видео одинаковые → берём min_log_mad
    assert robust_z(1000 * math.e, flat, CFG) == pytest.approx(1 / (MAD_SCALE * CFG.min_log_mad))


def test_baseline_small_sample_unreliable(now):
    samples = [Sample(f"v{i}", 1000, 10) for i in range(3)]
    b = compute_baseline("c", VideoFormat.LONG, samples, CFG, now)
    assert b.sample_size == 3 and not b.reliable and b.median_views == 1000
    assert compute_baseline("c", VideoFormat.LONG, [], CFG, now) is None


def test_leave_one_out_and_limit():
    mature = [Sample(f"v{i}", 100, 10) for i in range(40)]
    picked = pick_samples(mature, CFG, exclude="v0")
    assert len(picked) == CFG.baseline_videos and "v0" not in {s.video_id for s in picked}


def test_unreliable_factor():
    assert combine_score(8, 3, None, False, CFG) == pytest.approx(
        combine_score(8, 3, None, True, CFG) * 0.6
    )
    assert combine_score(0.5, -1, None, False, CFG) < 0  # отрицательный не «улучшается»


def test_views_at_age_interpolation(now):
    pub = now - timedelta(days=3)
    snaps = [
        VideoSnapshot(video_id="v", views=v, collected_at=pub + timedelta(hours=h))
        for h, v in ((12, 1200), (24, 2000), (48, 3000))
    ]
    assert views_at_age(pub, snaps, 6) == pytest.approx(600)  # от (0,0)
    assert views_at_age(pub, snaps, 36) == pytest.approx(2500)
    assert views_at_age(pub, snaps, 60) is None  # дальше снимков не экстраполируем
    one_old = [VideoSnapshot(video_id="v", views=9000, collected_at=pub + timedelta(days=30))]
    assert views_at_age(pub, one_old, 24) is None  # один далёкий снимок — не история


def outlier_ids(db):
    return {o.video_id for o in db.list_outliers()}


def test_planted_outliers_found(db, now):
    """Acceptance Фазы 2: на синтетике находятся именно заложенные аутлайеры."""
    cfg = AppConfig()
    # A: длинные ~10k + один ×9; шортсы ~3k + один ×12 (форматы раздельно)
    mature_channel(db, "A", now, base=10_000, n=20, seed=1)
    mature_channel(db, "A", now, base=3_000, n=15, fmt=VideoFormat.SHORT, seed=2)
    add_video(db, "A", "A-OUT-LONG", now - timedelta(days=15), [(now, 90_000)])
    add_video(db, "A", "A-OUT-SHORT", now - timedelta(days=20), [(now, 36_000)], VideoFormat.SHORT)
    # B: шумный канал без аутлайеров
    mature_channel(db, "B", now, base=50_000, n=25, sigma=0.3, seed=3)
    # C: свежий взлёт по скорости — 2 дня, ×5 к типичной кривой канала
    mature_channel(
        db, "C", now, base=8_000, n=12, seed=4, spacing_days=2, start_days=3, history=True
    )
    pub = now - timedelta(days=2)
    add_video(
        db,
        "C",
        "C-FAST",
        pub,
        [
            (pub + timedelta(hours=h), int(5 * 8_000 * growth(h / 24)))
            for h in (6, 12, 18, 24, 30, 36, 42, 48)
        ],
    )
    # D: маленький канал, выборка мала → low_confidence; маленький абсолютный порог
    mature_channel(db, "D", now, base=200, n=4, seed=5, subs=800)
    add_video(db, "D", "D-TINY", now - timedelta(days=10), [(now, 900)])

    r = score_all(db, cfg, now)
    assert outlier_ids(db) == {"A-OUT-LONG", "A-OUT-SHORT", "C-FAST"}, r

    long_o = db.get_outlier("A-OUT-LONG")
    assert (
        long_o.ratio > 7 and "high_ratio" in long_o.reason_flags and "high_z" in long_o.reason_flags
    )
    fast = db.get_outlier("C-FAST")
    assert "fast_start" in fast.reason_flags and "early_signal" in fast.reason_flags
    assert fast.velocity_ratio == pytest.approx(5, rel=0.25)

    # базлайны раздельные по формату
    bl = db.get_baseline("A", VideoFormat.LONG)
    bs = db.get_baseline("A", VideoFormat.SHORT)
    assert 7_000 < bl.median_views < 14_000 and 2_000 < bs.median_views < 4_500
    assert not db.get_baseline("D", VideoFormat.LONG).reliable


def test_small_channel_breakout_flag(db, now):
    cfg = AppConfig()
    mature_channel(db, "S", now, base=1_500, n=12, seed=7, subs=4_000)
    add_video(db, "S", "S-BREAK", now - timedelta(days=12), [(now, 45_000)])
    score_all(db, cfg, now)
    o = db.get_outlier("S-BREAK")
    assert o and {"small_channel_breakout", "views_exceed_subs", "high_ratio"} <= set(
        o.reason_flags
    )


def test_low_confidence_reduces_score(db, now):
    cfg = AppConfig()
    mature_channel(db, "U", now, base=20_000, n=5, seed=8)
    add_video(db, "U", "U-BIG", now - timedelta(days=10), [(now, 400_000)])
    score_all(db, cfg, now)
    o = db.get_outlier("U-BIG")
    assert o and "low_confidence" in o.reason_flags


def test_rescore_keeps_detected_at(db, now):
    cfg = AppConfig()
    mature_channel(db, "A", now, base=10_000, n=20, seed=1)
    add_video(db, "A", "A-OUT", now - timedelta(days=15), [(now, 90_000)])
    score_all(db, cfg, now)
    first = db.get_outlier("A-OUT").detected_at
    later = now + timedelta(hours=6)
    db.add_snapshots([VideoSnapshot(video_id="A-OUT", views=95_000, collected_at=later)])
    score_all(db, cfg, later)
    o = db.get_outlier("A-OUT")
    assert o.detected_at == first and o.views == 95_000


def test_fixture_outliers(app, now):
    """На фикстурах YouTube находятся три заложенных аутлайера."""
    from radar.collect.watchlist import poll_watchlist
    from radar.schemas import ChannelStatus

    for ref in ("@techguru", "@smallbuilder"):
        ch = app.youtube.resolve_channel(ref, purpose="t", now=now, status=ChannelStatus.WATCHING)
        app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)
    assert outlier_ids(app.db) == {"tgLONGOUT01", "tgSHORTOUT1", "sbBREAKOUT1"}
