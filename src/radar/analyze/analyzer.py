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
from radar.llm.base import LLMError, LLMProvider, extract_json, supports_batch
from radar.log import get_logger
from radar.schemas import (
    Analysis,
    AnalysisDraft,
    ChannelProfile,
    ChannelStatus,
    ImageInput,
    LLMRequest,
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
RECENT_IDEAS = 15
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
        "profile": profile.model_dump(mode="json", exclude={"channel"}),
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
            "niches": [
                {"id": n.id, "name": n.name}
                for nid in (channel.niche_ids if channel else [])
                if (n := self.db.get_niche(nid))
            ],
            "my_channel": self.profile.model_dump(mode="json", exclude={"channel"}),
            "already_planned_ideas": [t.title for t in self.db.list_topic_suggestions()][
                -RECENT_IDEAS:
            ],
        }

    def _check_budget(self, now: datetime) -> None:
        if self.spent_today(now) >= self.cfg.analysis.max_cost_usd_per_day:
            raise AnalysisBudgetExceeded(
                f"лимит ${self.cfg.analysis.max_cost_usd_per_day:.2f}/сутки на LLM исчерпан"
            )

    def prepare(self, video: Video, now: datetime) -> tuple[str, list[ImageInput]]:
        """Промпт и картинка для LLM: тратит 1 ед. квоты на комментарии."""
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
        return prompt, images

    def _save(
        self, video_id: str, draft: AnalysisDraft, model: str, cost: float, h: str, now: datetime
    ) -> Analysis:
        analysis = Analysis(
            **draft.model_dump(),
            video_id=video_id,
            model=model,
            cost=round(cost, 6),
            input_hash=h,
            created_at=now,
        )
        self.db.save_analysis(analysis)
        log.info("analysis_saved", video_id=video_id, cost=analysis.cost, model=model)
        return analysis

    def analyze(self, video_id: str, now: datetime, *, force: bool = False) -> Analysis:
        video = self.db.get_video(video_id)
        if video is None:
            raise LLMError(f"видео {video_id} нет в БД (сначала radar poll)")
        if not force and (hit := self.cached(video)):
            return hit
        self._check_budget(now)
        prompt, images = self.prepare(video, now)
        draft, model, cost = self._ask(prompt, images, now, video.id)
        h = input_hash(video, self.profile, self.llm.model, self.system)
        return self._save(video.id, draft, model, cost, h, now)

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
        flying = self.in_flight()
        watching = {c.id for c in self.db.list_channels(status=ChannelStatus.WATCHING)}
        for o in self.db.list_outliers(
            since=now - PENDING_WINDOW, min_score=self.cfg.analysis.score_threshold
        ):
            if o.channel_id not in watching or o.video_id in flying:  # скрытые — не тратят LLM
                continue
            v = self.db.get_video(o.video_id)
            if v and self.cached(v) is None and not self._failed_recently(v.id, now):
                out.append(o.video_id)
        return out

    def _failed_recently(self, video_id: str, now: datetime) -> bool:
        raw = self.db.get_kv(f"analysis_failed:{video_id}")
        return bool(raw) and now - parse_dt(raw) < FAILURE_BACKOFF  # type: ignore[arg-type]

    def in_flight(self) -> set[str]:
        return {vid for _, _, items in self.db.open_llm_batches() for vid in items}

    def awaiting(self, now: datetime) -> bool:
        """Есть что анализировать или ждём результаты пакета (для tick и ожидания дайджеста)."""
        return bool(self.db.open_llm_batches()) or bool(self.pending(now))

    def analyze_pending(self, now: datetime) -> TaskResult:
        if self.cfg.analysis.use_batch and supports_batch(self.llm):
            return self._batch_tick(now)
        if supports_batch(self.llm) and self.db.open_llm_batches():
            self.collect_batches(now)  # пакеты, поданные до выключения use_batch
        return self._sync_pending(now)

    # --- Batch API -----------------------------------------------------------------

    def _batch_tick(self, now: datetime) -> TaskResult:
        collected, failed = self.collect_batches(now)
        todo = self.pending(now)
        stats: dict[str, int | float | str] = {
            "collected": collected,
            "failed": failed,
            "submitted": 0,
        }
        if not todo:
            stats["in_flight"] = len(self.in_flight())
            return TaskResult(name="analyze", stats=stats)
        try:
            self._check_budget(now)
        except AnalysisBudgetExceeded as e:
            return TaskResult(name="analyze", stats=stats, deferred=True, message=str(e))
        requests: list[LLMRequest] = []
        hashes: dict[str, str] = {}
        deferred_msg = ""
        for vid in todo[: self.cfg.analysis.batch_max_items]:
            video = self.db.get_video(vid)
            if video is None:
                continue
            try:
                prompt, images = self.prepare(video, now)
            except (QuotaDeferred, QuotaExceededError) as e:
                deferred_msg = str(e)
                break
            requests.append(
                LLMRequest(
                    custom_id=vid,
                    system=self.system,
                    prompt=prompt,
                    images=images,
                    max_tokens=self.cfg.llm.max_tokens,
                )
            )
            hashes[vid] = input_hash(video, self.profile, self.llm.model, self.system)
        if requests:
            try:
                batch_id = self.llm.submit_batch(requests)  # type: ignore[attr-defined]
            except LLMError as e:
                return TaskResult(name="analyze", stats=stats, deferred=True, message=str(e))
            self.db.add_llm_batch(batch_id, hashes, now)
            stats["submitted"] = len(requests)
            log.info("analysis_batch_submitted", batch_id=batch_id, items=len(requests))
        stats["in_flight"] = len(self.in_flight())
        return TaskResult(
            name="analyze", stats=stats, deferred=bool(deferred_msg), message=deferred_msg
        )

    def collect_batches(self, now: datetime) -> tuple[int, int]:
        """Забрать готовые пакеты: валидные ответы → Analysis, остальные → бэкофф 24 ч."""
        collected = failed = 0
        timeout = timedelta(hours=self.cfg.analysis.batch_timeout_hours)
        for batch_id, created, items in self.db.open_llm_batches():
            expired = now - created > timeout
            try:
                ended = self.llm.batch_ended(batch_id)  # type: ignore[attr-defined]
                results = self.llm.batch_results(batch_id) if ended else []  # type: ignore[attr-defined]
            except LLMError as e:
                log.warning("analysis_batch_check_failed", batch_id=batch_id, error=str(e)[:200])
                if not expired:
                    continue
                ended, results = False, []
            if not ended and not expired:
                continue
            seen: set[str] = set()
            for r in results:
                if r.custom_id not in items:
                    continue
                seen.add(r.custom_id)
                if r.response:
                    self._record_usage(r.response, r.custom_id, now)
                draft = None
                if r.response and not r.error:
                    try:
                        draft = AnalysisDraft.model_validate(extract_json(r.response.text))
                    except (LLMError, ValidationError):
                        draft = None
                if draft and r.response:
                    self._save(
                        r.custom_id,
                        draft,
                        r.response.model,
                        r.response.cost,
                        items[r.custom_id],
                        now,
                    )
                    collected += 1
                else:
                    self.db.set_kv(f"analysis_failed:{r.custom_id}", iso(now))
                    failed += 1
                    log.warning(
                        "analysis_batch_item_failed",
                        video_id=r.custom_id,
                        error=(r.error or "invalid")[:200],
                    )
            for vid in set(items) - seen:  # пакет истёк или ответа нет
                self.db.set_kv(f"analysis_failed:{vid}", iso(now))
                failed += 1
            self.db.close_llm_batch(batch_id, now, "ended" if ended else "expired")
        return collected, failed

    def _sync_pending(self, now: datetime) -> TaskResult:
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
