"""Тренды по нишам: какие признаки чаще у аутлайеров, чем у всех видео (lift), и как это меняется.

Плюс разбор фидбэка «Не то» → рекомендации по порогам в отдельный файл (конфиг не меняется).
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from radar.bot.notifier import TEXT_LIMIT as TELEGRAM_TEXT_LIMIT
from radar.config import AppConfig
from radar.db import Database
from radar.delivery import give_up_delivery
from radar.digest.render import join_limited
from radar.export.suggestions import HINTS_FILE, write_yaml
from radar.log import get_logger
from radar.schemas import (
    ChannelStatus,
    FeedbackAction,
    NicheTrends,
    OutMessage,
    TaskResult,
    ThresholdRecommendation,
    TrendFeature,
    TrendReport,
    Video,
    VideoFormat,
)
from radar.timeutil import ensure_utc, iso

if TYPE_CHECKING:
    from radar.app import App

log = get_logger(__name__)

RECOMMENDATIONS_FILE = "threshold_recommendations.yaml"
WEEKLY_KEY = "trends_weekly_sent"
MIN_LIFT = 1.2
MIN_FEATURE_OUTLIERS = 2

_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]")
_CAPS_RE = re.compile(r"\b[A-ZА-ЯЁ]{3,}\b")

TITLE_FEATURES: dict[str, Callable[[str], bool]] = {
    "число в заголовке": lambda t: bool(re.search(r"\d", t)),
    "начинается с числа": lambda t: bool(re.match(r"\s*\d", t)),
    "вопрос": lambda t: "?" in t,
    "восклицание": lambda t: "!" in t,
    "скобки/уточнение": lambda t: bool(re.search(r"[\(\[]", t)),
    "слово капсом": lambda t: bool(_CAPS_RE.search(t)),
    "от первого лица": lambda t: bool(re.search(r"(?i)\b(я|мой|моя|мне|меня|my|i)\b", t)),
    "как/how to": lambda t: bool(re.search(r"(?i)\b(как|how to|how)\b", t)),
    "эмодзи": lambda t: bool(_EMOJI_RE.search(t)),
    "длинный (>60 симв.)": lambda t: len(t) > 60,
    "короткий (<35 симв.)": lambda t: len(t) < 35,
}

DURATION_BUCKETS = [
    (60, "< 1 мин"),
    (300, "1–5 мин"),
    (600, "5–10 мин"),
    (1200, "10–20 мин"),
    (2400, "20–40 мин"),
    (10**9, "40+ мин"),
]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
TIME_BUCKETS = [(6, "ночь 0–6"), (12, "утро 6–12"), (18, "день 12–18"), (24, "вечер 18–24")]
CATEGORY_NAMES = {
    "topic": "Темы",
    "title": "Заголовки",
    "format": "Форматы",
    "duration": "Длительность",
    "weekday": "День публикации",
    "time": "Время публикации",
}


_WORD_RE = re.compile(r"[a-zа-яё][a-zа-яё0-9+#-]{2,}", re.I)
# Слова короче 4 букв отсекаются в topic_stems, здесь только более длинные.
STOPWORDS = {
    "меня",
    "когда",
    "если",
    "только",
    "даже",
    "была",
    "были",
    "будет",
    "быть",
    "очень",
    "можно",
    "нужно",
    "свой",
    "свои",
    "своя",
    "самый",
    "самые",
    "часть",
    "день",
    "дней",
    "года",
    "with",
    "your",
    "this",
    "that",
    "from",
    "what",
    "into",
    "video",
    "shorts",
}
STEM_LEN = 6


def topic_stems(v: Video) -> dict[str, str]:
    """Темы ролика: слова заголовка и теги → {основа: словоформа}. Основа — первые 6 букв."""
    words = _WORD_RE.findall(v.title) + [w for tag in v.tags[:15] for w in _WORD_RE.findall(tag)]
    out: dict[str, str] = {}
    for w in words:
        lw = w.lower().strip("-")
        if len(lw) < 4 or lw in STOPWORDS:
            continue
        out.setdefault(lw[:STEM_LEN], lw)
    return out


def video_features(
    v: Video, tz: ZoneInfo, stems: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    feats = [("title", name) for name, fn in TITLE_FEATURES.items() if fn(v.title)]
    feats += [("topic", stem) for stem in (topic_stems(v) if stems is None else stems)]
    feats.append(("format", "shorts" if v.format == VideoFormat.SHORT else "длинные"))
    feats.append(
        ("duration", next(label for limit, label in DURATION_BUCKETS if v.duration_sec < limit))
    )
    local = ensure_utc(v.published_at).astimezone(tz)
    feats.append(("weekday", WEEKDAYS[local.weekday()]))
    feats.append(("time", next(label for limit, label in TIME_BUCKETS if local.hour < limit)))
    return feats


def _lift(n_feat_out: int, n_out: int, n_feat_all: int, n_all: int) -> float:
    """Сглаженный lift: доля признака среди аутлайеров / доля среди всех видео."""
    return ((n_feat_out + 0.5) / (n_out + 1)) / ((n_feat_all + 0.5) / (n_all + 1))


def _period_counts(
    videos: list[Video],
    outlier_ids: set[str],
    tz: ZoneInfo,
    forms: Counter[tuple[str, str]] | None = None,
) -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]], int, int]:
    """Счётчики признаков; forms (если передан) копит пары (основа темы, словоформа)."""
    all_c: Counter[tuple[str, str]] = Counter()
    out_c: Counter[tuple[str, str]] = Counter()
    n_out = 0
    for v in videos:
        stems = topic_stems(v)
        if forms is not None:
            forms.update(stems.items())
        feats = video_features(v, tz, stems)
        all_c.update(feats)
        if v.id in outlier_ids:
            out_c.update(feats)
            n_out += 1
    return all_c, out_c, len(videos), n_out


def build_trends(db: Database, cfg: AppConfig, now: datetime, days: int) -> TrendReport:
    tz = ZoneInfo(cfg.digest.timezone)
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    outlier_ids = {o.video_id for o in db.list_outliers(min_score=cfg.scoring.score_threshold)}
    report = TrendReport(generated_at=now, days=days)
    recent = db.list_videos(published_after=prev_start)
    for niche in db.list_niches(enabled_only=True):
        chans = {c.id for c in db.list_channels(status=ChannelStatus.WATCHING, niche_id=niche.id)}
        vids = [v for v in recent if v.channel_id in chans]
        cur = [v for v in vids if v.published_at >= start]
        prev = [v for v in vids if v.published_at < start]
        forms: Counter[tuple[str, str]] = Counter()
        all_c, out_c, n_all, n_out = _period_counts(cur, outlier_ids, tz, forms)
        p_all, p_out, pn_all, pn_out = _period_counts(prev, outlier_ids, tz)
        display: dict[str, str] = {}
        for (stem, word), _ in forms.most_common():
            display.setdefault(stem, word)
        features = []
        for key, n_feat_out in out_c.items():
            prev_lift = _lift(p_out[key], pn_out, p_all[key], pn_all) if pn_out else None
            features.append(
                TrendFeature(
                    category=key[0],
                    name=display.get(key[1], key[1]) if key[0] == "topic" else key[1],
                    n_outliers=n_feat_out,
                    n_all=all_c[key],
                    outlier_share=round(n_feat_out / n_out, 3),
                    base_share=round(all_c[key] / n_all, 3),
                    lift=round(_lift(n_feat_out, n_out, all_c[key], n_all), 3),
                    prev_lift=round(prev_lift, 3) if prev_lift is not None else None,
                )
            )
        features.sort(key=lambda f: (-f.lift, -f.n_outliers))
        report.niches.append(
            NicheTrends(
                niche_id=niche.id,
                niche_name=niche.name,
                days=days,
                n_videos=n_all,
                n_outliers=n_out,
                features=features,
            )
        )
    return report


def growing(nt: NicheTrends) -> list[TrendFeature]:
    return [f for f in nt.features if f.lift >= MIN_LIFT and f.n_outliers >= MIN_FEATURE_OUTLIERS]


def render_trends_text(
    report: TrendReport, summary: str | None = None, limit: int = TELEGRAM_TEXT_LIMIT
) -> str:
    lines = [f"📈 <b>Тренды за {report.days} дн.</b>"]
    if summary:
        lines.append(escape(summary))
    if not report.niches:
        lines.append("Ниш нет.")
    for nt in report.niches:
        lines.append(
            f"\n<b>{escape(nt.niche_name)}</b>: видео {nt.n_videos}, аутлайеров {nt.n_outliers}"
        )
        g = growing(nt)
        if not g:
            lines.append("  заметных паттернов нет (мало аутлайеров)")
            continue
        for cat in CATEGORY_NAMES:
            items = [f for f in g if f.category == cat][:4]
            if not items:
                continue
            parts = []
            for f in items:
                delta = ""
                if f.prev_lift is not None:
                    delta = (
                        " ↑"
                        if f.lift > f.prev_lift * 1.1
                        else (" ↓" if f.lift < f.prev_lift * 0.9 else " →")
                    )
                parts.append(
                    f"{escape(f.name)} ×{f.lift:.1f} ({f.n_outliers}/{nt.n_outliers}){delta}"
                )
            lines.append(f"  {CATEGORY_NAMES[cat]}: " + "; ".join(parts))
    return join_limited(lines, limit)


def content_hints(report: TrendReport) -> dict[str, object]:
    out: dict[str, object] = {
        "generated_at": iso(report.generated_at),
        "days": report.days,
        "niches": {},
    }
    for nt in report.niches:
        g = growing(nt)
        out["niches"][nt.niche_id] = {  # type: ignore[index]
            "name": nt.niche_name,
            "videos": nt.n_videos,
            "outliers": nt.n_outliers,
            **{
                key: [
                    {
                        "pattern": f.name,
                        "lift": f.lift,
                        "outliers": f.n_outliers,
                        "prev_lift": f.prev_lift,
                    }
                    for f in g
                    if f.category == cat
                ]
                for cat, key in (
                    ("title", "title_patterns"),
                    ("format", "formats"),
                    ("duration", "durations"),
                    ("weekday", "publish_weekdays"),
                    ("time", "publish_times"),
                    ("topic", "topics"),
                )
            },
        }
    return out


def write_content_hints(report: TrendReport, exports_dir: Path) -> Path:
    path = exports_dir / HINTS_FILE
    write_yaml(path, content_hints(report))
    return path


# --- фидбэк «Не то» → рекомендации ---------------------------------------------------


def threshold_recommendations(db: Database, cfg: AppConfig) -> list[ThresholdRecommendation]:
    """Сравнение аутлайеров с «Не то» и остальных показанных в дайджесте. Только советы."""
    sc = cfg.scoring
    bad_ids = {f.video_id for f in db.list_feedback(FeedbackAction.NOT_RELEVANT)}
    bad = [o for vid in bad_ids if (o := db.get_outlier(vid))]
    shown = db.digested_video_ids()
    good = [o for vid in shown - bad_ids if (o := db.get_outlier(vid))]
    recs: list[ThresholdRecommendation] = []
    if len(bad) < 3:
        return recs
    n = len(bad)

    def share(items: list, flag: str) -> float:
        return sum(flag in o.reason_flags for o in items) / len(items) if items else 0.0

    bad_scores = sorted(o.score for o in bad)
    good_median = statistics.median(o.score for o in good) if good else None
    bad_median = statistics.median(bad_scores)
    if bad_median < sc.score_threshold + 1.0 and (good_median is None or bad_median < good_median):
        p75 = bad_scores[min(len(bad_scores) - 1, int(0.75 * len(bad_scores)))]
        recs.append(
            ThresholdRecommendation(
                param="scoring.score_threshold",
                current=sc.score_threshold,
                suggested=round(max(sc.score_threshold + 0.5, p75) * 2) / 2,
                reason=f"медианный score «Не то» {bad_median:.2f} — у самого порога"
                + (f" (у остальных {good_median:.2f})" if good_median is not None else ""),
                evidence_n=n,
            )
        )
    if (
        share(bad, "low_confidence") >= 0.5
        and share(bad, "low_confidence") > share(good, "low_confidence") + 0.2
    ):
        recs.append(
            ThresholdRecommendation(
                param="scoring.unreliable_factor",
                current=sc.unreliable_factor,
                suggested=round(max(0.2, sc.unreliable_factor - 0.2), 2),
                reason=f"{share(bad, 'low_confidence'):.0%} «Не то» — каналы с малой выборкой (low_confidence)",
                evidence_n=n,
            )
        )
    if (
        share(bad, "velocity_fallback") >= 0.5
        and share(bad, "velocity_fallback") > share(good, "velocity_fallback") + 0.2
    ):
        recs.append(
            ThresholdRecommendation(
                param="scoring.weight_velocity",
                current=sc.weight_velocity,
                suggested=round(max(0.2, sc.weight_velocity - 0.3), 2),
                reason=f"{share(bad, 'velocity_fallback'):.0%} «Не то» сработали по скорости без истории снимков",
                evidence_n=n,
            )
        )
    bad_views = statistics.median(o.views for o in bad)
    if bad_views < 3 * sc.min_views:
        recs.append(
            ThresholdRecommendation(
                param="scoring.min_views",
                current=sc.min_views,
                suggested=int(round(bad_views * 1.5, -2)),
                reason=f"медиана просмотров «Не то» всего {int(bad_views)}",
                evidence_n=n,
            )
        )
    fmt_counts = Counter(o.format.value for o in bad)
    fmt, cnt = fmt_counts.most_common(1)[0]
    if cnt / n >= 0.7 and n >= 4:
        recs.append(
            ThresholdRecommendation(
                param="niches[].format",
                current="both",
                suggested="long" if fmt == "short" else "shorts",
                reason=f"{cnt} из {n} «Не то» — формат {fmt}; возможно, он не нужен в нише",
                evidence_n=n,
            )
        )
    for channel_id, c in Counter(o.channel_id for o in bad).items():
        if c >= 3:
            ch = db.get_channel(channel_id)
            recs.append(
                ThresholdRecommendation(
                    param="channel.hide",
                    current=channel_id,
                    suggested="hidden",
                    reason=f"{c} раз «Не то» у канала {ch.title if ch else channel_id}: radar channel hide {channel_id}",
                    evidence_n=c,
                )
            )
    return recs


def write_recommendations(
    recs: list[ThresholdRecommendation], exports_dir: Path, now: datetime
) -> Path:
    path = exports_dir / RECOMMENDATIONS_FILE
    write_yaml(
        path,
        {
            "generated_at": iso(now),
            "note": "Рекомендации по порогам на основе кнопки «Не то». Конфиг автоматически не меняется.",
            "recommendations": [r.model_dump(mode="json") for r in recs],
        },
    )
    return path


def render_recommendations(recs: list[ThresholdRecommendation]) -> str:
    if not recs:
        return "Рекомендаций по порогам нет (нужно ≥ 3 отметок «Не то»)."
    lines = ["🛠 <b>Рекомендации по порогам</b> (конфиг не меняется)"]
    lines += [
        f"• {escape(r.param)}: {r.current} → {r.suggested} — {escape(r.reason)}" for r in recs
    ]
    return join_limited(lines, 4096)


# --- еженедельный отчёт ----------------------------------------------------------------


def _week_id(now: datetime, cfg: AppConfig) -> str:
    y, w, _ = ensure_utc(now).astimezone(ZoneInfo(cfg.digest.timezone)).isocalendar()
    return f"{y}-W{w:02d}"


def weekly_report_due(db: Database, cfg: AppConfig, now: datetime) -> bool:
    local = ensure_utc(now).astimezone(ZoneInfo(cfg.digest.timezone))
    if local.weekday() != cfg.trends.weekly_weekday or local.hour < cfg.trends.weekly_hour:
        return False
    return db.get_kv(WEEKLY_KEY) != _week_id(now, cfg)


def llm_summary(app: App, report: TrendReport, now: datetime) -> str | None:
    from radar.analyze.analyzer import load_prompt
    from radar.llm.base import LLMError

    payload = {
        "niches": [
            {
                "niche": nt.niche_name,
                "videos": nt.n_videos,
                "outliers": nt.n_outliers,
                "growing": [f.model_dump() for f in growing(nt)][:15],
            }
            for nt in report.niches
        ],
        "my_channel": app.profile.model_dump(mode="json") if app.profile else None,
    }
    try:
        resp = app.llm.complete(
            system=load_prompt("trends"),
            prompt=json.dumps(payload, ensure_ascii=False),
            max_tokens=app.config.llm.light_max_tokens,
            effort=app.config.llm.light_effort,
        )
    except LLMError as e:
        log.warning("trends_summary_failed", error=str(e)[:200])
        if r := e.response:
            app.db.add_llm_usage("trends", r.model, r.input_tokens, r.output_tokens, r.cost, now)
        return None
    app.db.add_llm_usage(
        "trends", resp.model, resp.input_tokens, resp.output_tokens, resp.cost, now
    )
    return resp.text.strip()[:1500] or None


def run_trends(
    app: App, now: datetime, days: int, *, with_llm: bool
) -> tuple[TrendReport, list[ThresholdRecommendation], str | None]:
    """Построить тренды, записать content_hints.yaml и threshold_recommendations.yaml."""
    report = build_trends(app.db, app.config, now, days)
    write_content_hints(report, app.settings.exports_dir)
    recs = threshold_recommendations(app.db, app.config)
    write_recommendations(recs, app.settings.exports_dir, now)
    summary = llm_summary(app, report, now) if with_llm and app.has_llm() else None
    return report, recs, summary


def send_weekly_report(app: App, now: datetime) -> TaskResult:
    report, recs, summary = run_trends(
        app, now, app.config.trends.days, with_llm=app.config.trends.use_llm
    )
    msgs = [OutMessage(text=render_trends_text(report, summary))]
    if recs:
        msgs.append(OutMessage(text=render_recommendations(recs)))
    sent = app.notifier.send(msgs)
    week = _week_id(now, app.config)
    if sent == 0 and not give_up_delivery(app.db, f"trends:{week}"):
        return TaskResult(
            name="trends_weekly",
            stats={"niches": len(report.niches), "recommendations": len(recs), "sent": 0},
            deferred=True,
            message="ни одно сообщение не доставлено",
        )
    app.db.set_kv(WEEKLY_KEY, week)
    return TaskResult(
        name="trends_weekly",
        stats={"niches": len(report.niches), "recommendations": len(recs), "sent": sent},
    )
