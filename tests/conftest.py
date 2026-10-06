from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar import cli
from radar.app import App
from radar.bot.notifier import FakeNotifier
from radar.config import Settings
from radar.db import Database
from radar.llm.fake import FakeLLM
from radar.youtube.fake import FakeYouTubeClient

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    for p in (ROOT / "config").glob("*.example.yaml"):
        shutil.copy(p, d / p.name.replace(".example", ""))
    return d


@pytest.fixture
def settings(tmp_path: Path, config_dir: Path) -> Settings:
    return Settings(
        _env_file=None,
        radar_data_dir=tmp_path / "data",
        radar_config_dir=config_dir,
        admin_ids=[111],
        youtube_api_key=None,
        anthropic_api_key=None,
        telegram_bot_token=None,
    )


@pytest.fixture
def db() -> Iterator[Database]:
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def fake_yt() -> FakeYouTubeClient:
    return FakeYouTubeClient.from_fixtures(FIXTURES)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def app(settings, db, fake_yt, fake_llm, notifier) -> App:
    a = App.build(settings, db=db, youtube_client=fake_yt, llm=fake_llm, notifier=notifier)
    a.sync_niches(NOW)
    return a


@pytest.fixture
def cli_app(app: App, monkeypatch: pytest.MonkeyPatch) -> Iterator[App]:
    monkeypatch.setenv("RADAR_NOW", NOW.isoformat())
    cli.reset_app()
    monkeypatch.setattr(cli, "app_factory", lambda: app)
    yield app
    cli.reset_app()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Любая попытка реального HTTP в тестах — ошибка (MockTransport не затрагивается)."""
    import httpx

    def deny(*a: object, **kw: object) -> None:
        raise AssertionError("реальный сетевой запрос в тесте")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)
    try:
        import httpx2

        monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", deny)
        monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", deny)
    except (ImportError, AttributeError):
        pass
