"""Настройки из env (секреты) и YAML-конфиги (поведение)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from radar.schemas import ChannelProfile, Niche


class Settings(BaseSettings):
    """Секреты и пути. Читаются из окружения и .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    youtube_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    telegram_bot_token: SecretStr | None = None
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    radar_data_dir: Path = Path("data")
    radar_config_dir: Path = Path("config")
    radar_fake: bool = False
    radar_fixtures_dir: Path = Path("tests/fixtures")
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def db_path(self) -> Path:
        return self.radar_data_dir / "radar.db"

    @property
    def exports_dir(self) -> Path:
        return self.radar_data_dir / "exports"

    @property
    def cache_dir(self) -> Path:
        return self.radar_data_dir / "cache"

    def secret_values(self) -> list[str]:
        return [
            s.get_secret_value()
            for s in (self.youtube_api_key, self.anthropic_api_key, self.telegram_bot_token)
            if s is not None and s.get_secret_value()
        ]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuotaConfig(_Cfg):
    daily_budget: int = 10_000
    discovery_reserve: int = 2_000
    safety_margin: int = 200
    costs: dict[str, int] = Field(
        default_factory=lambda: {
            "videos.list": 1,
            "channels.list": 1,
            "playlistItems.list": 1,
            "commentThreads.list": 1,
            "search.list": 100,
        }
    )
    expensive_threshold: int = 50


class SnapshotConfig(_Cfg):
    fresh_days: int = 14
    fresh_interval_hours: float = 6
    mid_days: int = 60
    mid_interval_hours: float = 24


class FormatConfig(_Cfg):
    short_max_sec: int = 180


class ScoringConfig(_Cfg):
    baseline_videos: int = 30
    baseline_min_age_days: float = 7
    min_sample: int = 8
    min_log_mad: float = 0.15
    min_views: int = 1_000
    weight_ratio: float = 1.0
    weight_z: float = 0.5
    weight_velocity: float = 0.7
    unreliable_factor: float = 0.6
    score_threshold: float = 3.0
    flag_ratio: float = 3.0
    flag_z: float = 2.5
    flag_velocity: float = 2.0
    small_channel_subs: int = 10_000
    velocity_min_videos: int = 3
    velocity_max_age_days: float = 14


class AnalysisConfig(_Cfg):
    score_threshold: float = 3.5
    max_per_tick: int = 5
    comments_count: int = 20
    max_cost_usd_per_day: float = 1.0
    use_thumbnails: bool = True


class LLMConfig(_Cfg):
    max_tokens: int = 4_000
    input_usd_per_mtok: float = 4.0
    output_usd_per_mtok: float = 20.0
    effort: str | None = "medium"
    refusal_fallback: bool = True


class DiscoveryConfig(_Cfg):
    auto_approve: bool = False
    published_within_days: int = 30
    max_results: int = 50


class WatchlistConfig(_Cfg):
    interval_minutes: int = 60
    channel_refresh_hours: int = 24


class DigestConfig(_Cfg):
    timezone: str = "Europe/Moscow"
    send_hour: int = Field(default=8, ge=0, le=23)
    top_n_per_niche: int = 5
    max_items: int = 15
    lookback_hours: int = 48
    wait_analysis_hours: int = Field(default=2, ge=0, le=12)
    use_llm_intro: bool = False


class TrendsConfig(_Cfg):
    days: int = 7
    weekly_weekday: int = Field(default=0, ge=0, le=6, description="0 = понедельник")
    weekly_hour: int = Field(default=10, ge=0, le=23)
    use_llm: bool = False


class AppConfig(_Cfg):
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    snapshots: SnapshotConfig = Field(default_factory=SnapshotConfig)
    formats: FormatConfig = Field(default_factory=FormatConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    trends: TrendsConfig = Field(default_factory=TrendsConfig)


def resolve_config_file(config_dir: Path, name: str) -> Path | None:
    """config/<name>.yaml, иначе config/<name>.example.yaml."""
    for candidate in (config_dir / f"{name}.yaml", config_dir / f"{name}.example.yaml"):
        if candidate.exists():
            return candidate
    return None


def _read_yaml(path: Path | None) -> Any:
    if path is None:
        return None
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_app_config(config_dir: Path) -> AppConfig:
    data = _read_yaml(resolve_config_file(config_dir, "settings")) or {}
    return AppConfig.model_validate(data)


def load_niches(config_dir: Path) -> list[Niche]:
    data = _read_yaml(resolve_config_file(config_dir, "niches")) or {}
    items = data.get("niches", []) if isinstance(data, dict) else data
    niches = [Niche.model_validate(n) for n in items or []]
    ids = [n.id for n in niches]
    if len(ids) != len(set(ids)):
        raise ValueError("id ниш в niches.yaml должны быть уникальны")
    return niches


def load_profile(config_dir: Path) -> ChannelProfile | None:
    data = _read_yaml(resolve_config_file(config_dir, "channel_profile"))
    return ChannelProfile.model_validate(data) if data else None
