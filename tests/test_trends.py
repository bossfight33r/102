from datetime import UTC, datetime, timedelta

import yaml

from radar.schemas import (
    Channel,
    ChannelStatus,
    Digest,
    DigestItem,
    Feedback,
    FeedbackAction,
    Outlier,
    Video,
    VideoFormat,
)
from radar.trends import (
    build_trends,
    growing,
    render_trends_text,
    run_trends,
    send_weekly_report,
    threshold_recommendations,
    weekly_report_due,
)


def seed_niche(db, now):
    db.upsert_channel(
        Channel(
            id="C1",
            title="C1",
            subs=50_000,
            uploads_playlist_id="UU1",
            niche_ids=["ai-tools"],
            status=ChannelStatus.WATCHING,
            added_at=now,
        ),
        now,
        keep_status=False,
    )
    # текущая неделя: 12 обычных длинных без чисел + 4 аутлайера-шортса с числом в заголовке
    for i in range(12):
        pub = now - timedelta(days=1 + i * 0.5)
        db.upsert_video(
            Video(
                id=f"n{i}",
                channel_id="C1",
                title="Обзор инструмента",
                duration_sec=900,
                format=VideoFormat.LONG,
                published_at=pub,
            ),
            now,
        )
    for i in range(4):
        pub = now - timedelta(days=2 + i)
        vid = f"o{i}"
        db.upsert_video(
            Video(
                id=vid,
                channel_id="C1",
                title=f"{i + 5} нейросетей за 60 секунд",
                duration_sec=45,
                format=VideoFormat.SHORT,
                published_at=pub,
            ),
            now,
        )
        db.upsert_outlier(
            Outlier(
                video_id=vid,
                channel_id="C1",
                format=VideoFormat.SHORT,
                views=50_000,
                ratio=8,
                z_score=4,
                score=6,
                reason_flags=["high_ratio"],
                detected_at=now,
            ),
            now,
        )
    # прошлая неделя: аутлайер длинный без числа
    db.upsert_video(
        Video(
            id="p0",
            channel_id="C1",
            title="Обзор старый",
            duration_sec=900,
            format=VideoFormat.LONG,
            published_at=now - timedelta(days=10),
        ),
        now,
    )
    db.upsert_video(
        Video(
            id="p1",
            channel_id="C1",
            title="Ещё обзор",
            duration_sec=900,
            format=VideoFormat.LONG,
            published_at=now - timedelta(days=11),
        ),
        now,
    )
    db.upsert_outlier(
        Outlier(
            video_id="p0",
            channel_id="C1",
            format=VideoFormat.LONG,
            views=50_000,
            ratio=8,
            z_score=4,
            score=6,
            detected_at=now - timedelta(days=9),
        ),
        now,
    )


def test_trends_growing_patterns(app, now):
    seed_niche(app.db, now)
    report = build_trends(app.db, app.config, now, days=7)
    nt = report.niches[0]
    assert nt.n_videos == 16 and nt.n_outliers == 4
    names = {(f.category, f.name) for f in growing(nt)}
    assert ("title", "число в заголовке") in names
    assert ("format", "shorts") in names
    assert ("duration", "< 1 мин") in names
    assert ("format", "длинные") not in names
    assert ("topic", "нейросетей") in names and ("topic", "обзор") not in names
    shorts = next(f for f in nt.features if f.name == "shorts")
    assert shorts.prev_lift is not None and shorts.lift > shorts.prev_lift
    text = render_trends_text(report)
    assert "Тренды за 7" in text and "число в заголовке" in text and "↑" in text


def test_run_trends_writes_exports(app, now):
    seed_niche(app.db, now)
    run_trends(app, now, 7, with_llm=False)
    hints = yaml.safe_load((app.settings.exports_dir / "content_hints.yaml").read_text())
    niche = hints["niches"]["ai-tools"]
    assert any(p["pattern"] == "shorts" for p in niche["formats"])
    assert niche["title_patterns"] and "publish_times" in niche
    recs = yaml.safe_load((app.settings.exports_dir / "threshold_recommendations.yaml").read_text())
    assert recs["recommendations"] == []


def _not_relevant(db, now, n, **outlier_kw):
    items = []
    for i in range(n):
        vid = f"bad{i}"
        kw = dict(
            video_id=vid,
            channel_id="CB",
            format=VideoFormat.SHORT,
            views=1_500,
            ratio=3,
            z_score=2,
            score=3.2,
            reason_flags=["low_confidence", "velocity_fallback"],
            detected_at=now,
        )
        kw.update(outlier_kw)
        db.upsert_outlier(Outlier(**kw), now)
        db.add_feedback(Feedback(video_id=vid, action=FeedbackAction.NOT_RELEVANT, at=now))
        items.append(vid)
    for i in range(4):
        vid = f"good{i}"
        db.upsert_outlier(
            Outlier(
                video_id=vid,
                channel_id="CG",
                format=VideoFormat.LONG,
                views=80_000,
                ratio=10,
                z_score=5,
                score=7,
                reason_flags=["high_ratio"],
                detected_at=now,
            ),
            now,
        )
        items.append(vid)
    db.save_digest(
        Digest(
            date=now.date(),
            items=[
                DigestItem(
                    video_id=v,
                    niche_id="x",
                    niche_name="x",
                    title="t",
                    channel_title="c",
                    url="u",
                    format=VideoFormat.LONG,
                    ratio=1,
                    views=1,
                    age_days=1,
                    score=1,
                )
                for v in items
            ],
        )
    )


def test_threshold_recommendations(app, now, config_dir):
    before = (config_dir / "settings.yaml").read_text()
    assert threshold_recommendations(app.db, app.config) == []  # нет фидбэка
    _not_relevant(app.db, now, 4)
    recs = {r.param: r for r in threshold_recommendations(app.db, app.config)}
    assert {
        "scoring.score_threshold",
        "scoring.unreliable_factor",
        "scoring.weight_velocity",
        "scoring.min_views",
        "niches[].format",
        "channel.hide",
    } <= set(recs)
    assert recs["scoring.score_threshold"].suggested > app.config.scoring.score_threshold
    assert recs["niches[].format"].suggested == "long"
    run_trends(app, now, 7, with_llm=False)
    data = yaml.safe_load((app.settings.exports_dir / "threshold_recommendations.yaml").read_text())
    assert len(data["recommendations"]) == len(recs)
    assert (config_dir / "settings.yaml").read_text() == before  # конфиг не трогаем


def test_weekly_report(app, notifier, fake_llm):
    monday = datetime(2026, 10, 12, 7, 30, tzinfo=UTC)  # пн 10:30 MSK
    assert weekly_report_due(app.db, app.config, monday)
    assert not weekly_report_due(app.db, app.config, monday - timedelta(hours=1))  # 09:30 MSK
    assert not weekly_report_due(app.db, app.config, monday + timedelta(days=1))  # вторник
    seed_niche(app.db, monday)
    app.config.trends.use_llm = True
    r = send_weekly_report(app, monday)
    assert (
        r.stats["sent"] == 1
        and "Тренды" in notifier.sent[0].text
        and "Fake summary" in notifier.sent[0].text
    )
    assert not weekly_report_due(app.db, app.config, monday + timedelta(hours=2))
    assert weekly_report_due(app.db, app.config, monday + timedelta(days=7))


def test_trends_cli_and_bot(cli_app, now):
    from typer.testing import CliRunner

    from radar.bot.handlers import cmd_trends
    from radar.cli import app as cli

    seed_niche(cli_app.db, now)
    r = CliRunner().invoke(cli, ["trends", "--days", "7"])
    assert r.exit_code == 0, r.output
    assert "число в заголовке" in r.output and "content_hints.yaml" in r.output
    assert "Тренды" in cmd_trends(cli_app, now)
