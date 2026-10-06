import json
from datetime import timedelta

import pytest

from radar.analyze.analyzer import AnalysisBudgetExceeded, Analyzer, load_prompt
from radar.analyze.thumbnails import ThumbnailStore
from radar.collect.watchlist import poll_watchlist
from radar.llm.base import LLMError
from radar.llm.fake import DEFAULT_ANALYSIS, FakeLLM
from radar.schemas import ChannelStatus
from radar.score.outliers import score_all

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


@pytest.fixture
def collected(app, now):
    for ref in ("@techguru", "@smallbuilder"):
        ch = app.youtube.resolve_channel(ref, purpose="t", now=now, status=ChannelStatus.WATCHING)
        app.db.upsert_channel(ch, now, keep_status=False)
    poll_watchlist(app.youtube, app.db, app.config, now)
    score_all(app.db, app.config, now)
    return app


@pytest.fixture
def fetched(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return JPEG

    return ThumbnailStore(tmp_path / "thumbs", fetch=fetch), calls


def make(app, llm, thumbs=None):
    return Analyzer(app.db, app.youtube, llm, app.config, app.profile, thumbs)


def test_analysis_valid_and_cached(collected, fake_yt, fetched, now):
    """Acceptance Фазы 3: FakeLLM → валидный Analysis; повторный запуск не вызывает LLM."""
    store, thumb_calls = fetched
    llm = FakeLLM(cost=0.02)
    a = make(collected, llm, store).analyze("tgLONGOUT01", now)
    assert a.video_id == "tgLONGOUT01" and a.model == "fake-llm" and a.cost == pytest.approx(0.02)
    assert a.idea_for_my_channel.difference_from_original
    assert len(llm.calls) == 1 and llm.calls[0]["images"] == 1
    payload = llm.calls[0]["prompt"]
    assert "А какая из них работает бесплатно?" in payload  # комментарии
    assert "автоматизация рутины" in payload  # мой профиль
    assert "views_over_time" in payload and "chapters" in payload
    assert collected.db.llm_cost_since(now - timedelta(days=1)) == pytest.approx(0.02)

    comments_calls = len(fake_yt.calls_of("commentThreads.list"))
    again = make(collected, llm, store).analyze("tgLONGOUT01", now + timedelta(hours=3))
    assert again == a and len(llm.calls) == 1
    assert len(fake_yt.calls_of("commentThreads.list")) == comments_calls  # без квоты
    assert len(thumb_calls) == 1


def test_cache_invalidated_by_input_change(collected, now):
    llm = FakeLLM()
    an = make(collected, llm)
    an.analyze("tgLONGOUT01", now)
    v = collected.db.get_video("tgLONGOUT01")
    collected.db.upsert_video(v.model_copy(update={"title": "Новый заголовок"}), now)
    an.analyze("tgLONGOUT01", now)
    assert len(llm.calls) == 2
    an.analyze("tgLONGOUT01", now, force=True)
    assert len(llm.calls) == 3


def test_invalid_then_valid_retries(collected, now):
    answers = iter(["не json", json.dumps(DEFAULT_ANALYSIS)])
    llm = FakeLLM(responder=lambda s, p: next(answers), cost=0.01)
    a = make(collected, llm).analyze("tgLONGOUT01", now)
    assert len(llm.calls) == 2 and "не прошёл валидацию" in llm.calls[1]["prompt"]
    assert a.cost == pytest.approx(0.02)


def test_schema_violation_fails_and_not_saved(collected, now):
    bad = dict(DEFAULT_ANALYSIS, extra_field="x")
    llm = FakeLLM(responder=lambda s, p: json.dumps(bad))
    with pytest.raises(LLMError):
        make(collected, llm).analyze("tgLONGOUT01", now)
    assert collected.db.get_analysis("tgLONGOUT01") is None
    assert len(llm.calls) == 2


def test_daily_cost_budget(collected, now):
    collected.config.analysis.max_cost_usd_per_day = 0.015
    llm = FakeLLM(cost=0.01)
    an = make(collected, llm)
    an.analyze("tgLONGOUT01", now)
    an.analyze("tgSHORTOUT1", now)  # 0.01 < 0.015 — ещё можно
    with pytest.raises(AnalysisBudgetExceeded):
        an.analyze("sbBREAKOUT1", now)
    assert len(llm.calls) == 2


def test_analyze_pending(collected, now):
    llm = FakeLLM()
    an = make(collected, llm)
    assert set(an.pending(now)) == {"tgLONGOUT01", "tgSHORTOUT1", "sbBREAKOUT1"}
    collected.config.analysis.max_per_tick = 2
    r = an.analyze_pending(now)
    assert r.stats["done"] == 2 and r.stats["pending"] == 1
    r2 = an.analyze_pending(now)
    assert r2.stats["done"] == 1 and an.pending(now) == []
    assert len(llm.calls) == 3


def test_failed_analysis_backoff(collected, now):
    llm = FakeLLM(responder=lambda s, p: "мусор")
    an = make(collected, llm)
    collected.config.analysis.max_per_tick = 1
    r = an.analyze_pending(now)
    assert r.stats["failed"] == 1
    calls = len(llm.calls)
    an.analyze_pending(now + timedelta(minutes=15))
    # упавшее видео не повторяется сутки, берётся следующее
    assert len(llm.calls) == calls + 2


def test_prompt_requires_adaptation_not_copy():
    p = load_prompt("analyze")
    assert "не копия" in p.lower() or "не копир" in p.lower()
    assert "ANALYSIS_JSON" in p and "difference_from_original" in p


def test_thumbnail_store(tmp_path, collected):
    calls = []
    store = ThumbnailStore(tmp_path, fetch=lambda u: calls.append(u) or JPEG)
    v = collected.db.get_video("tgLONGOUT01")
    assert store.get(v).media_type == "image/jpeg"
    assert store.get(v) is not None and len(calls) == 1  # из кеша
    evil = v.model_copy(update={"id": "x", "thumbnail_url": "https://evil.example.com/a.jpg"})
    store.get(evil)
    assert all("evil" not in u for u in calls)

    def boom(url):
        raise ConnectionError

    assert ThumbnailStore(tmp_path / "other", fetch=boom).get(v) is None
