from datetime import timedelta

import pytest

from radar.analyze.analyzer import Analyzer
from radar.collect.watchlist import poll_watchlist
from radar.digest.build import build_digest, digest_due, local_date, send_digest
from radar.digest.render import render_card, render_digest
from radar.schemas import ChannelStatus, Feedback, FeedbackAction, FormatPref
from radar.score.outliers import score_all


@pytest.fixture
def ready(app, now):
    for ref in ("@techguru", "@smallbuilder"):
        ch = app.youtube.resolve_channel(
            ref, purpose="t", now=now, niche_ids=["ai-tools"], status=ChannelStatus.WATCHING
        )
        app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)
    Analyzer(app.db, app.youtube, app.llm, app.config, app.profile).analyze_pending(now)
    return app


def test_digest_build_on_fixtures(ready, now):
    """Acceptance Фазы 4: дайджест собирается на фикстурах."""
    d = build_digest(ready.db, ready.config, now)
    assert d.date == local_date(now, ready.config)
    ids = [i.video_id for i in d.items]
    assert set(ids) == {"tgLONGOUT01", "tgSHORTOUT1", "sbBREAKOUT1"}
    assert [i.score for i in d.items] == sorted((i.score for i in d.items), reverse=True)
    item = d.items[0]
    assert item.why_short and item.idea and item.niche_name == "AI-инструменты для работы"
    card = render_card(item)
    assert f"×{item.ratio:.1f}" in card.text and "youtu.be" in card.text and "🎯 Идея" in card.text
    assert card.photo_url and len([b for row in card.buttons for b in row]) == 4
    assert (
        build_digest(ready.db, ready.config, now + timedelta(hours=1)) == d
    )  # тот же день — тот же


def test_digest_html_escaped(ready, now):
    v = ready.db.get_video("tgLONGOUT01")
    ready.db.upsert_video(v.model_copy(update={"title": "<script>x</script> & co"}), now)
    d = build_digest(ready.db, ready.config, now)
    text = "".join(m.text for m in render_digest(d))
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_send_once_and_next_day_excludes(ready, notifier, now):
    r = send_digest(ready.db, ready.config, notifier, now)
    assert r.stats["sent"] == 4  # заголовок + 3 карточки
    assert (
        send_digest(ready.db, ready.config, notifier, now + timedelta(hours=2)).stats["sent"] == 0
    )
    assert len(notifier.sent) == 4
    nxt = build_digest(ready.db, ready.config, now + timedelta(days=1))
    assert nxt.items == []  # уже отправленные не повторяются
    assert "нет" in render_digest(nxt)[0].text


def test_feedback_and_hidden_filtering(ready, now):
    ready.db.add_feedback(
        Feedback(video_id="tgSHORTOUT1", action=FeedbackAction.NOT_RELEVANT, at=now)
    )
    ready.db.set_channel_status("UCsmallbuilder0000000000", ChannelStatus.HIDDEN)
    d = build_digest(ready.db, ready.config, now)
    assert [i.video_id for i in d.items] == ["tgLONGOUT01"]


def test_niche_format_filter_and_top_n(ready, now):
    n = ready.db.get_niche("ai-tools")
    ready.db.upsert_niche(n.model_copy(update={"format": FormatPref.LONG}), now)
    ready.config.digest.top_n_per_niche = 1
    d = build_digest(ready.db, ready.config, now)
    assert len(d.items) == 1 and d.items[0].format.value == "long"


def test_digest_due_by_local_hour(app, now):
    # now = 06:00 UTC = 09:00 MSK ≥ 8
    assert digest_due(app.db, app.config, now)
    early = now.replace(hour=4)  # 07:00 MSK
    assert not digest_due(app.db, app.config, early)


def test_llm_intro(ready, notifier, fake_llm, now):
    ready.config.digest.use_llm_intro = True
    send_digest(ready.db, ready.config, notifier, now, llm=fake_llm, profile=ready.profile)
    assert "Fake summary" in notifier.sent[0].text
