"""Контракты между модулями. Единственное место для структур, которые передаются между пакетами."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- перечисления -------------------------------------------------------------


class FormatPref(StrEnum):
    SHORTS = "shorts"
    LONG = "long"
    BOTH = "both"


class VideoFormat(StrEnum):
    SHORT = "short"
    LONG = "long"


class ChannelStatus(StrEnum):
    CANDIDATE = "candidate"
    WATCHING = "watching"
    HIDDEN = "hidden"


class FeedbackAction(StrEnum):
    TO_TOPICS = "to_topics"
    HIDE_CHANNEL = "hide_channel"
    NOT_RELEVANT = "not_relevant"
    DETAILS = "details"


# --- конфигурируемые сущности ---------------------------------------------------


class Niche(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$")
    name: str
    language: str = "ru"
    region: str = "RU"
    seed_queries: list[str] = Field(default_factory=list)
    seed_channels: list[str] = Field(default_factory=list)
    format: FormatPref = FormatPref.BOTH
    min_subs: int = Field(default=1_000, ge=0)
    max_subs: int = Field(default=1_000_000, ge=0)
    discovery_per_day: int = Field(default=2, ge=0, description="search.list вызовов в сутки")
    enabled: bool = True

    @model_validator(mode="after")
    def _check_subs(self) -> Niche:
        if self.min_subs > self.max_subs:
            raise ValueError("min_subs должен быть <= max_subs")
        return self


class ChannelProfile(Model):
    """Мой канал: под него адаптируются идеи."""

    name: str = ""
    language: str = "ru"
    topics: list[str]
    audience_level: str
    style: str
    can_show: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


# --- данные YouTube -----------------------------------------------------------


class Channel(Model):
    id: str
    title: str
    handle: str | None = None
    subs: int | None = None
    uploads_playlist_id: str
    niche_ids: list[str] = Field(default_factory=list)
    status: ChannelStatus = ChannelStatus.CANDIDATE
    added_at: datetime


class Chapter(Model):
    start_sec: int
    title: str


class Video(Model):
    id: str
    channel_id: str
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    duration_sec: int
    format: VideoFormat
    published_at: datetime
    thumbnail_url: str | None = None
    chapters: list[Chapter] = Field(default_factory=list)


class VideoStats(Model):
    """Статистика из videos.list в момент запроса (ещё не снимок)."""

    video_id: str
    views: int
    likes: int | None = None
    comments: int | None = None


class VideoSnapshot(Model):
    video_id: str
    views: int
    likes: int | None = None
    comments: int | None = None
    collected_at: datetime


class SearchHit(Model):
    video_id: str
    channel_id: str
    published_at: datetime | None = None
    title: str = ""


class Comment(Model):
    text: str
    likes: int = 0


# --- скоринг -----------------------------------------------------------------


class ChannelBaseline(Model):
    channel_id: str
    format: VideoFormat
    median_views: float
    mad: float = Field(description="MAD натурального логарифма просмотров (без масштаба 1.4826)")
    sample_size: int
    reliable: bool
    computed_at: datetime
    median_views_per_day: float | None = None


class Outlier(Model):
    video_id: str
    channel_id: str
    format: VideoFormat
    views: int
    ratio: float
    z_score: float
    velocity_ratio: float | None = None
    score: float
    reason_flags: list[str] = Field(default_factory=list)
    detected_at: datetime


# --- анализ ------------------------------------------------------------------


class WhyItWorked(Model):
    title_pattern: str
    thumbnail_elements: list[str] = Field(default_factory=list)
    topic: str
    format: str
    duration: str


class IdeaForMyChannel(Model):
    title: str
    pitch: str
    difference_from_original: str
    key_points: list[str] = Field(default_factory=list)


class AnalysisDraft(Model):
    """То, что возвращает LLM. Строгая схема — лишние поля запрещены."""

    why_it_worked: WhyItWorked
    hook_formula: str
    audience_questions: list[str] = Field(default_factory=list)
    idea_for_my_channel: IdeaForMyChannel
    short_form_angle: str
    confidence: float = Field(ge=0.0, le=1.0)


class Analysis(AnalysisDraft):
    video_id: str
    model: str
    cost: float = Field(ge=0.0, description="USD")
    input_hash: str
    created_at: datetime


# --- LLM ---------------------------------------------------------------------


class ImageInput(Model):
    media_type: str = "image/jpeg"
    data: bytes


class LLMResponse(Model):
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


class LLMRequest(Model):
    """Один запрос пакета (Batch API). custom_id — ^[A-Za-z0-9_-]{1,64}$."""

    custom_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    system: str
    prompt: str
    images: list[ImageInput] = Field(default_factory=list)
    max_tokens: int = 4000
    effort: str | None = None


class BatchItemResult(Model):
    custom_id: str
    response: LLMResponse | None = None
    error: str | None = None


# --- квота -------------------------------------------------------------------


class QuotaEntry(Model):
    date: date
    method: str
    units: int
    purpose: str
    at: datetime | None = None


# --- дайджест, фидбэк, экспорт -------------------------------------------------


class DigestItem(Model):
    video_id: str
    niche_id: str
    niche_name: str
    title: str
    channel_title: str
    url: str
    thumbnail_url: str | None = None
    format: VideoFormat
    ratio: float
    views: int
    age_days: float
    score: float
    reason_flags: list[str] = Field(default_factory=list)
    why_short: str | None = None
    idea: str | None = None


class Digest(Model):
    date: date
    items: list[DigestItem] = Field(default_factory=list)
    sent_at: datetime | None = None


class Feedback(Model):
    video_id: str
    action: FeedbackAction
    at: datetime


class TopicSuggestion(Model):
    title: str
    why: str
    source_video_ids: list[str]
    audience_level: str
    key_points: list[str] = Field(default_factory=list)


# --- исходящие сообщения (Notifier) ---------------------------------------------


class Button(Model):
    text: str
    callback_data: str = Field(max_length=64)


class OutMessage(Model):
    text: str
    photo_url: str | None = None
    buttons: list[list[Button]] = Field(default_factory=list)


# --- результаты задач ---------------------------------------------------------


class TaskResult(Model):
    name: str
    stats: dict[str, int | float | str] = Field(default_factory=dict)
    deferred: bool = False
    message: str = ""


# --- тренды и рекомендации ----------------------------------------------------------


class TrendFeature(Model):
    category: str  # title | format | duration | weekday | time
    name: str
    n_outliers: int
    n_all: int
    outlier_share: float
    base_share: float
    lift: float
    prev_lift: float | None = None


class NicheTrends(Model):
    niche_id: str
    niche_name: str
    days: int
    n_videos: int
    n_outliers: int
    features: list[TrendFeature] = Field(default_factory=list)


class TrendReport(Model):
    generated_at: datetime
    days: int
    niches: list[NicheTrends] = Field(default_factory=list)


class ThresholdRecommendation(Model):
    param: str
    current: float | int | str | None = None
    suggested: float | int | str | None = None
    reason: str
    evidence_n: int
