"""Сборка зависимостей: БД, YouTube, LLM, Notifier. Подмена фейками — через аргументы build()."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from radar.bot.notifier import ConsoleNotifier, Notifier, TelegramNotifier
from radar.config import AppConfig, Settings, load_app_config, load_niches, load_profile
from radar.db import Database
from radar.llm.base import LLMProvider
from radar.log import configure_logging
from radar.schemas import ChannelProfile, Niche
from radar.timeutil import parse_dt, utcnow
from radar.youtube.client import YouTube, YouTubeClient
from radar.youtube.quota import QuotaPlanner

if TYPE_CHECKING:
    from radar.analyze.analyzer import Analyzer


class ConfigError(Exception):
    """Не хватает ключа или конфига для запрошенной операции."""


@dataclass
class App:
    settings: Settings
    config: AppConfig
    db: Database
    yaml_niches: list[Niche]
    profile: ChannelProfile | None
    _yt_client: YouTubeClient | None = None
    _llm: LLMProvider | None = None
    _notifier: Notifier | None = None
    _youtube: YouTube | None = field(default=None, init=False)

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        db: Database | None = None,
        youtube_client: YouTubeClient | None = None,
        llm: LLMProvider | None = None,
        notifier: Notifier | None = None,
        configure_logs: bool = False,
    ) -> App:
        settings = settings or Settings()
        if configure_logs:
            configure_logging(settings.log_level, settings.log_json, settings.secret_values())
        cfg_dir = settings.radar_config_dir
        return cls(
            settings=settings,
            config=load_app_config(cfg_dir),
            db=db or Database(settings.db_path),
            yaml_niches=load_niches(cfg_dir),
            profile=load_profile(cfg_dir),
            _yt_client=youtube_client,
            _llm=llm,
            _notifier=notifier,
        )

    @property
    def planner(self) -> QuotaPlanner:
        return QuotaPlanner(self.db, self.config.quota)

    @property
    def youtube(self) -> YouTube:
        if self._youtube is None:
            self._youtube = YouTube(self._make_yt_client(), self.planner, self.config.formats)
        return self._youtube

    def _make_yt_client(self) -> YouTubeClient:
        if self._yt_client is not None:
            return self._yt_client
        if self.settings.radar_fake:
            from radar.youtube.fake import FakeYouTubeClient

            return FakeYouTubeClient.from_fixtures(self.settings.radar_fixtures_dir)
        key = self.settings.youtube_api_key
        if key is None or not key.get_secret_value():
            raise ConfigError("YOUTUBE_API_KEY не задан (см. .env.example)")
        from radar.youtube.api import HttpYouTubeClient

        return HttpYouTubeClient(key.get_secret_value())

    @property
    def llm(self) -> LLMProvider:
        if self._llm is None:
            if self.settings.radar_fake:
                from radar.llm.fake import FakeLLM

                self._llm = FakeLLM()
            else:
                key = self.settings.anthropic_api_key
                if key is None or not key.get_secret_value():
                    raise ConfigError("ANTHROPIC_API_KEY не задан")
                from radar.llm.anthropic import AnthropicLLM

                self._llm = AnthropicLLM(
                    key.get_secret_value(), self.settings.anthropic_model, self.config.llm
                )
        return self._llm

    def has_llm(self) -> bool:
        if self._llm is not None or self.settings.radar_fake:
            return True
        key = self.settings.anthropic_api_key
        return key is not None and bool(key.get_secret_value())

    @property
    def notifier(self) -> Notifier:
        if self._notifier is None:
            if self.settings.radar_fake:
                self._notifier = ConsoleNotifier()
            else:
                token = self.settings.telegram_bot_token
                if token is None or not token.get_secret_value():
                    raise ConfigError("TELEGRAM_BOT_TOKEN не задан")
                self._notifier = TelegramNotifier(token.get_secret_value(), self.settings.admin_ids)
        return self._notifier

    def analyzer(self) -> Analyzer:
        from radar.analyze.analyzer import Analyzer
        from radar.analyze.thumbnails import ThumbnailStore

        if self.profile is None:
            raise ConfigError("нет config/channel_profile.yaml — анализ невозможен")
        thumbs = ThumbnailStore(self.settings.cache_dir / "thumbs")
        if self.settings.radar_fake:
            thumbs.fetch = _no_network
        return Analyzer(self.db, self.youtube, self.llm, self.config, self.profile, thumbs)

    def sync_niches(self, now: datetime) -> list[Niche]:
        """Ниши из niches.yaml upsert-ятся в БД (YAML побеждает); добавленные через CLI остаются."""
        with self.db.tx():
            for n in self.yaml_niches:
                self.db.upsert_niche(n, now)
        return self.db.list_niches()


def _no_network(url: str) -> bytes:
    raise ConnectionError("RADAR_FAKE: сеть отключена")


def current_time() -> datetime:
    """Текущее время UTC. RADAR_NOW (ISO) переопределяет — для демо и отладки."""
    raw = os.environ.get("RADAR_NOW")
    return parse_dt(raw) if raw else utcnow()
