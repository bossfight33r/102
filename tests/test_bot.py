import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import yaml
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Update

from radar.bot.handlers import (
    AdminOnlyMiddleware,
    cmd_candidates,
    cmd_niches,
    cmd_quota,
    handle_candidate,
    handle_feedback,
    is_admin,
)
from radar.bot.main import build_dispatcher
from radar.schemas import ChannelStatus, FeedbackAction
from test_digest import ready  # noqa: F401  (фикстура)

ADMIN = 111


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def close(self):
        pass


def test_is_admin():
    assert is_admin(111, [111]) and not is_admin(222, [111]) and not is_admin(None, [111])


def test_middleware_blocks_non_admin():
    mw = AdminOnlyMiddleware([ADMIN])
    called = []

    async def handler(event, data):
        called.append(event)
        return "ok"

    stranger = SimpleNamespace(from_user=SimpleNamespace(id=999))
    assert asyncio.run(mw(handler, stranger, {})) is None
    admin = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN))
    assert asyncio.run(mw(handler, admin, {})) == "ok"
    assert called == [admin]


def test_handle_feedback_actions(ready, now):  # noqa: F811
    toast, extra = handle_feedback(ready, "t", "tgLONGOUT01", now)
    assert "темы" in toast and extra == []
    path = ready.settings.exports_dir / "topic_suggestions.yaml"
    data = yaml.safe_load(path.read_text())
    assert data["topics"][0]["source_video_ids"] == ["tgLONGOUT01"]
    handle_feedback(ready, "t", "tgLONGOUT01", now)  # повтор не дублирует
    assert len(yaml.safe_load(path.read_text())["topics"]) == 1

    toast, extra = handle_feedback(ready, "d", "tgLONGOUT01", now)
    assert "Идея для моего канала" in extra[0].text

    handle_feedback(ready, "h", "sbBREAKOUT1", now)
    assert ready.db.get_channel("UCsmallbuilder0000000000").status == ChannelStatus.HIDDEN

    handle_feedback(ready, "n", "tgSHORTOUT1", now)
    actions = [f.action for f in ready.db.list_feedback()]
    assert actions == [
        FeedbackAction.TO_TOPICS,
        FeedbackAction.TO_TOPICS,
        FeedbackAction.DETAILS,
        FeedbackAction.HIDE_CHANNEL,
        FeedbackAction.NOT_RELEVANT,
    ]
    assert handle_feedback(ready, "zz", "x", now)[0] == "Неизвестная кнопка"


def test_to_topics_without_analysis(app, now):
    toast, _ = handle_feedback(app, "t", "unknown", now)
    assert "Анализа ещё нет" in toast


def test_candidates_flow(app, now):
    from radar.collect.discovery import run_discovery

    run_discovery(app.youtube, app.db, app.config, app.db.list_niches(), now)
    msgs = cmd_candidates(app)
    assert len(msgs) == 1 + 3 and msgs[1].buttons[0][0].callback_data.startswith("ch:a:")
    cid = msgs[1].buttons[0][0].callback_data.split(":")[2]
    assert handle_candidate(app, "a", cid) == "✅ Следим"
    assert app.db.get_channel(cid).status == ChannelStatus.WATCHING
    assert handle_candidate(app, "a", "nope") == "Канал не найден"
    assert "Ниши" in cmd_niches(app) and "Квота" in cmd_quota(app, now)


def _callback_update(user_id, data):
    return Update.model_validate(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "chat_instance": "ci",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "U"},
            },
        }
    )


def _message_update(user_id, text):
    return Update.model_validate(
        {
            "update_id": 2,
            "message": {
                "message_id": 1,
                "date": int(datetime(2026, 10, 6, tzinfo=UTC).timestamp()),
                "text": text,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "U"},
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
            },
        }
    )


def test_dispatcher_only_admins(app, now, monkeypatch):
    monkeypatch.setenv("RADAR_NOW", now.isoformat())
    session = FakeSession()
    bot = Bot("123456:" + "A" * 35, session=session)
    dp = build_dispatcher(app)

    async def run():
        await dp.feed_update(bot, _callback_update(999, "fb:n:tgLONGOUT01"))
        await dp.feed_update(bot, _message_update(999, "/quota"))
        assert app.db.list_feedback() == [] and session.requests == []  # чужому — тишина
        await dp.feed_update(bot, _callback_update(ADMIN, "fb:n:tgLONGOUT01"))
        assert [f.action for f in app.db.list_feedback()] == [FeedbackAction.NOT_RELEVANT]
        await dp.feed_update(bot, _message_update(ADMIN, "/quota"))
        assert len(session.requests) == 2

    asyncio.run(run())
