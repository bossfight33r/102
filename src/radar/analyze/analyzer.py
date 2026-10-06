"""Анализ аутлайеров через LLM: метаданные + динамика + превью + комментарии + мой профиль → Analysis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from radar.analyze.comments import comments_for_prompt, fetch_top_comments
from radar.analyze.thumbnails import ThumbnailStore
from radar.config import AppConfig
from radar.db import Database
from radar.llm.base import LLMError, LLMProvider, extract_json
from radar.log import get_logger
from radar.schemas import (
    Analysis,
    AnalysisDraft,
    ChannelProfile,
    ChannelStatus,
    ImageInput,
    LLMResponse,
    TaskResult,
    Video,
    VideoSnapshot,
)
from radar.timeutil import age_days, iso, parse_dt
from radar.youtube.client import QuotaExceededError, YouTube
from radar.youtube.quota import QuotaDeferred

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PENDING_WINDOW = timedelta(days=7)
FAILURE_BACKOFF = timedelta(hours=24)
DESCRIPTION_LIMIT = 3_000


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


class AnalysisBudgetExceeded(Exception):
    """Дневной лимит расходов на LLM исчерпан."""


def input_hash(video: Video, profile: ChannelProfile, model: str, prompt: str) -> str:
    """Хеш стабильных входов. Просмотры и комментарии не входят: они меняются постоянно,
    а повторный анализ из-за прироста просмотров не нужен (ADR 0010)."""
    payload = {
        "video": video.model_dump(mode="json", exclude={"published_at"}),
        "profile": profile.model_dump(mode="json"),
        "model": model,
        "prompt": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def dynamics(
    video: Video, snaps: list[VideoSnapshot], max_points: int = 12
) -> list[dict[str, Any]]:
    pts = [
        {
            "age_hours": round((s.collected_at - video.published_at).total_seconds() / 3600, 1),
            "views": s.views,
        }
        for s in snaps
    ]
    if len(pts) <= max_points:
        return pts
    step = (len(pts) - 1) / (max_points - 1)
    return [pts[round(i * step)] for i in range(max_points)]


class Analyzer:
    def __init__(
        self,
        db: Database,
        yt: YouTube,
        llm: LLMProvider,
        cfg: AppConfig,
        profile: ChannelProfile,
        thumbnails: ThumbnailStore | None = None,
    ) -> None:
        self.db = db
        self.yt = yt
        self.llm = llm
        self.cfg = cfg
        self.profile = profile
        self.thumbnails = thumbnails
        self.system = load_prompt("analyze")

    def cached(self, video: Video) -> Analysis | None:
        a = self.db.get_analysis(video.id)
        if a and a.input_hash == input_hash(video, self.profile, self.llm.model, self.system):
            return a
        return None

    def spent_today(self, now: datetime) -> float:
        return self.db.llm_cost_since(now - timedelta(hours=24))

    def build_payload(
        self, video: Video, now: datetime, comments: list[dict[str, object]]
    ) -> dict[str, Any]:
        channel = self.db.get_channel(video.channel_id)
        outlier = self.db.get_outlier(video.id)
        baseline = self.db.get_baseline(video.channel_id, video.format)
        return {
            "video": {
                "title": video.title,
                "description": video.description[:DESCRIPTION_LIMIT],
                "tags": video.tags[:30],
                "duration_sec": video.duration_sec,
                "format": video.format.value,
                "age_days": round(age_days(video.published_at, now), 1),
                "published_at": iso(video.published_at),
                "chapters": [c.model_dump() for c in video.chapters],
            },
            "channel": {
                "title": channel.title if channel else None,
                "subscribers": channel.subs if channel else None,
                "median_views_same_format": round(baseline.median_views) if baseline else None,
            },
            "performance": {
                "views": outlier.views if outlier else None,
                "ratio_vs_channel_median": outlier.ratio if outlier else None,
                "z_score": outlier.z_score if outlier else None,
                "velocity_ratio": outlier.velocity_ratio if outlier else None,
                "reason_flags": outlier.reason_flags if outlier else [],
                "views_over_time": dynamics(video, self.db.snapshots_for(video.id)),
            },
            "top_comments": comments,
            "my_channel": self.profile.model_dump(mode="json"),
        }

    def analyze(self, video_id: str, now: datetime, *, force: bool = False) -> Analysis:
        video = self.db.get_video(video_id)
        if video is None:
            raise LLMError(f"видео {video_id} нет в БД (сначала radar poll)")
        if not force and (hit := self.cached(video)):
            return hit
        if self.spent_today(now) >= self.cfg.analysis.max_cost_usd_per_day:
            raise AnalysisBudgetExceeded(
                f"лимит ${self.cfg.analysis.max_cost_usd_per_day:.2f}/сутки на LLM исчерпан"
            )
        comments = fetch_top_comments(self.yt, video.id, self.cfg.analysis.comments_count, now)
        images: list[ImageInput] = []
        if self.cfg.analysis.use_thumbnails and self.thumbnails:
            img = self.thumbnails.get(video)
            if img:
                images.append(img)
        prompt = (
            "Данные ролика-аутлайера и моего канала (JSON):\n"
            + json.dumps(
                self.build_payload(video, now, comments_for_prompt(comments)),
                ensure_ascii=False,
                indent=1,
            )
            + ("\n\nПревью ролика приложено изображением." if images else "\n\nПревью недоступно.")
        )
        draft, model, cost = self._ask(prompt, images, now, video.id)
        analysis = Analysis(
            **draft.model_dump(),
            video_id=video.id,
            model=model,
            cost=round(cost, 6),
            input_hash=input_hash(video, self.profile, self.llm.model, self.system),
            created_at=now,
        )
        self.db.save_analysis(analysis)
        log.info("analysis_saved", video_id=video.id, cost=analysis.cost, model=model)
        return analysis

    def _ask(
        self, prompt: str, images: list[ImageInput], now: datetime, video_id: str
    ) -> tuple[AnalysisDraft, str, float]:
        """Запрос + строгая валидация. Одна повторная попытка с текстом ошибки."""
        cost = 0.0
        error = ""
        for attempt in range(2):
            p = (
                prompt
                if not error
                else (
                    f"{prompt}\n\nПредыдущий ответ не прошёл валидацию: {error[:500]}\n"
                    "Верни только JSON строго по схеме."
                )
            )
            try:
                resp = self.llm.complete(
                    system=self.system, prompt=p, images=images, max_tokens=self.cfg.llm.max_tokens
                )
            except LLMError as e:
                if e.response:  # оплаченный, но пустой ответ — учитываем в дневном лимите
                    self._record_usage(e.response, video_id, now)
                raise
            cost += resp.cost
            self._record_usage(resp, video_id, now)
            try:
                return AnalysisDraft.model_validate(extract_json(resp.text)), resp.model, cost
            except (LLMError, ValidationError) as e:
                error = str(e)
                log.warning("analysis_invalid", video_id=video_id, attempt=attempt + 1)
        raise LLMError(f"ответ LLM не соответствует схеме Analysis: {error[:300]}")

    def _record_usage(self, resp: LLMResponse, video_id: str, now: datetime) -> None:
        self.db.add_llm_usage(
            f"analysis:{video_id}",
            resp.model,
            resp.input_tokens,
            resp.output_tokens,
            resp.cost,
            now,
        )

    def pending(self, now: datetime) -> list[str]:
        """Аутлайеры выше порога анализа за последние 7 дней без актуального анализа."""
        out = []
        watching = {c.id for c in self.db.list_channels(status=ChannelStatus.WATCHING)}
        for o in self.db.list_outliers(
            since=now - PENDING_WINDOW, min_score=self.cfg.analysis.score_threshold
        ):
            if o.channel_id not in watching:  # скрытые каналы не тратят LLM
                continue
            v = self.db.get_video(o.video_id)
            if v and self.cached(v) is None and not self._failed_recently(v.id, now):
                out.append(o.video_id)
        return out

    def _failed_recently(self, video_id: str, now: datetime) -> bool:
        raw = self.db.get_kv(f"analysis_failed:{video_id}")
        return bool(raw) and now - parse_dt(raw) < FAILURE_BACKOFF  # type: ignore[arg-type]

    def analyze_pending(self, now: datetime) -> TaskResult:
        done = failed = 0
        todo = self.pending(now)
        for vid in todo[: self.cfg.analysis.max_per_tick]:
            try:
                self.analyze(vid, now)
                done += 1
            except AnalysisBudgetExceeded as e:
                return TaskResult(
                    name="analyze",
                    stats={"done": done, "pending": len(todo) - done},
                    deferred=True,
                    message=str(e),
                )
            except (QuotaDeferred, QuotaExceededError) as e:
                return TaskResult(
                    name="analyze",
                    stats={"done": done, "pending": len(todo) - done},
                    deferred=True,
                    message=str(e),
                )
            except LLMError as e:
                failed += 1
                self.db.set_kv(f"analysis_failed:{vid}", iso(now))
                log.warning("analysis_failed", video_id=vid, error=str(e)[:200])
        return TaskResult(
            name="analyze",
            stats={"done": done, "failed": failed, "pending": len(todo) - done - failed},
        )
