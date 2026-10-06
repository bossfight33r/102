"""CLI: radar <команда>."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Annotated

import typer

from radar.app import App, ConfigError, current_time
from radar.schemas import Analysis, ChannelStatus, FormatPref, Niche, TaskResult, VideoFormat

app = typer.Typer(
    help="Outlier Radar: аутлайеры YouTube → идеи для моего канала.", no_args_is_help=True
)
niche_app = typer.Typer(help="Ниши: radar niche add|list", no_args_is_help=True)
channel_app = typer.Typer(help="Каналы: radar channel add|list|approve|hide", no_args_is_help=True)
app.add_typer(niche_app, name="niche")
app.add_typer(channel_app, name="channel")

# Тесты подменяют фабрику, чтобы подставить фейки.
app_factory: Callable[[], App] = lambda: App.build(configure_logs=True)  # noqa: E731
_app: App | None = None


def get_app() -> App:
    global _app
    if _app is None:
        _app = app_factory()
        _app.sync_niches(current_time())
    return _app


def reset_app() -> None:
    global _app
    _app = None


def fail(msg: str, code: int = 1) -> None:
    typer.echo(f"Ошибка: {msg}", err=True)
    raise typer.Exit(code)


# --- ниши -----------------------------------------------------------------------


@niche_app.command("add")
def niche_add(
    niche_id: Annotated[str, typer.Argument(help="id: латиница, цифры, - и _")],
    name: Annotated[str, typer.Option("--name", help="Название")],
    query: Annotated[
        list[str], typer.Option("--query", "-q", help="Поисковый запрос (можно несколько)")
    ] = [],  # noqa: B006
    channel: Annotated[
        list[str], typer.Option("--channel", "-c", help="@handle или id канала")
    ] = [],  # noqa: B006
    language: str = "ru",
    region: str = "RU",
    fmt: Annotated[FormatPref, typer.Option("--format")] = FormatPref.BOTH,
    min_subs: int = 1_000,
    max_subs: int = 1_000_000,
    per_day: Annotated[int, typer.Option("--per-day", help="search.list вызовов в сутки")] = 2,
) -> None:
    """Добавить нишу в БД (ниши из config/niches.yaml синхронизируются автоматически)."""
    a = get_app()
    try:
        niche = Niche(
            id=niche_id,
            name=name,
            language=language,
            region=region,
            seed_queries=query,
            seed_channels=channel,
            format=fmt,
            min_subs=min_subs,
            max_subs=max_subs,
            discovery_per_day=per_day,
        )
    except ValueError as e:
        fail(str(e))
        return
    a.db.upsert_niche(niche, current_time())
    typer.echo(f"Ниша {niche.id} сохранена.")


@niche_app.command("list")
def niche_list() -> None:
    a = get_app()
    niches = a.db.list_niches()
    if not niches:
        typer.echo("Ниш нет. Добавьте в config/niches.yaml или radar niche add.")
    for n in niches:
        flag = "" if n.enabled else " [выкл]"
        typer.echo(
            f"{n.id}{flag}: {n.name} | {n.language}/{n.region} | {n.format.value} | "
            f"subs {n.min_subs}..{n.max_subs} | запросов {len(n.seed_queries)}, "
            f"каналов {len(n.seed_channels)}, search/день {n.discovery_per_day}"
        )


# --- каналы ---------------------------------------------------------------------


@channel_app.command("add")
def channel_add(
    ref: Annotated[str, typer.Argument(help="@handle, id (UC...) или ссылка на канал")],
    niche: Annotated[list[str], typer.Option("--niche", "-n", help="id ниши")] = [],  # noqa: B006
) -> None:
    """Добавить канал сразу в watchlist (1 ед. квоты)."""
    a = get_app()
    now = current_time()
    for n in niche:
        if not a.db.get_niche(n):
            fail(f"ниша {n} не найдена")
    try:
        ch = a.youtube.resolve_channel(ref, purpose="cli:channel_add", now=now, niche_ids=niche)
    except ConfigError as e:
        fail(str(e))
        return
    if ch is None:
        fail(f"канал {ref} не найден")
        return
    saved = a.db.upsert_channel(ch, now, keep_status=False)
    typer.echo(
        f"Канал {saved.title} ({saved.id}) → {saved.status.value}, подписчиков: {saved.subs}"
    )


@channel_app.command("list")
def channel_list(
    status: Annotated[ChannelStatus | None, typer.Option("--status")] = None,
    niche: Annotated[str | None, typer.Option("--niche")] = None,
) -> None:
    a = get_app()
    chans = a.db.list_channels(status=status, niche_id=niche)
    if not chans:
        typer.echo("Каналов нет.")
    for c in chans:
        typer.echo(
            f"{c.id} [{c.status.value}] {c.title} {c.handle or ''} | subs {c.subs} | "
            f"ниши: {','.join(c.niche_ids) or '-'}"
        )


def _set_status(channel_id: str, status: ChannelStatus) -> None:
    a = get_app()
    if not a.db.set_channel_status(channel_id, status):
        fail(f"канал {channel_id} не найден")
    typer.echo(f"{channel_id} → {status.value}")


@channel_app.command("approve")
def channel_approve(channel_id: str) -> None:
    """Кандидат → watching."""
    _set_status(channel_id, ChannelStatus.WATCHING)


@channel_app.command("hide")
def channel_hide(channel_id: str) -> None:
    """Скрыть канал (не собирать и не показывать)."""
    _set_status(channel_id, ChannelStatus.HIDDEN)


# --- сбор ----------------------------------------------------------------------


def _print_result(r: TaskResult) -> None:
    stats = ", ".join(f"{k}={v}" for k, v in r.stats.items())
    suffix = f" — отложено: {r.message}" if r.deferred else (f" — {r.message}" if r.message else "")
    typer.echo(f"{r.name}: {stats}{suffix}")


@app.command()
def discover() -> None:
    """Discovery по seed_queries ниш в пределах discovery_per_day и бюджета квоты."""
    from radar.collect.discovery import run_discovery

    a = get_app()
    _print_result(
        run_discovery(
            a.youtube, a.db, a.config, a.db.list_niches(enabled_only=True), current_time()
        )
    )
    typer.echo(
        f"Кандидатов всего: {len(a.db.list_channels(status=ChannelStatus.CANDIDATE))} (radar channel list --status candidate)"
    )


@app.command()
def poll(
    force: Annotated[
        bool, typer.Option("--force", help="Опросить все каналы, не глядя на интервал")
    ] = False,
) -> None:
    """Сбор: seed-каналы, новые видео watchlist, снимки статистики."""
    from radar.collect.snapshots import collect_snapshots
    from radar.collect.watchlist import add_seed_channels, poll_watchlist

    a = get_app()
    now = current_time()
    _print_result(add_seed_channels(a.youtube, a.db, a.db.list_niches(enabled_only=True), now))
    _print_result(poll_watchlist(a.youtube, a.db, a.config, now, force=force))
    _print_result(collect_snapshots(a.youtube, a.db, a.config, now))


# --- скоринг --------------------------------------------------------------------


@app.command()
def score() -> None:
    """Пересчитать базлайны и найти аутлайеры."""
    from radar.score.outliers import score_all

    a = get_app()
    _print_result(score_all(a.db, a.config, current_time()))


@app.command()
def outliers(
    niche: Annotated[str | None, typer.Option("--niche", help="id ниши")] = None,
    fmt: Annotated[VideoFormat | None, typer.Option("--format", help="short | long")] = None,
    days: Annotated[int, typer.Option("--days", help="Обнаруженные за N дней")] = 7,
    limit: int = 30,
) -> None:
    """Аутлайеры по убыванию score."""
    from datetime import timedelta

    a = get_app()
    now = current_time()
    rows = a.db.list_outliers(since=now - timedelta(days=days), fmt=fmt)
    shown = 0
    for o in rows:
        ch = a.db.get_channel(o.channel_id)
        if niche and (ch is None or niche not in ch.niche_ids):
            continue
        v = a.db.get_video(o.video_id)
        vel = f" vel×{o.velocity_ratio:.1f}" if o.velocity_ratio else ""
        typer.echo(
            f"{o.score:6.2f}  ×{o.ratio:<6.1f} z={o.z_score:<5.1f}{vel}  {o.views:>9,} просм. "
            f"[{o.format.value}] {v.title if v else o.video_id} — {ch.title if ch else o.channel_id}\n"
            f"        https://youtu.be/{o.video_id}  {', '.join(o.reason_flags)}"
        )
        shown += 1
        if shown >= limit:
            break
    if not shown:
        typer.echo("Аутлайеров нет (radar poll → radar score).")


# --- анализ ---------------------------------------------------------------------


def format_analysis(an: Analysis) -> str:
    w = an.why_it_worked
    idea = an.idea_for_my_channel
    lines = [
        f"Почему зашло: {w.title_pattern}; тема — {w.topic}; формат — {w.format}; длительность — {w.duration}",
        f"Превью: {', '.join(w.thumbnail_elements) or '—'}",
        f"Хук: {an.hook_formula}",
        "Вопросы зрителей: " + ("; ".join(an.audience_questions) or "—"),
        f"Идея для моего канала: {idea.title}",
        f"  {idea.pitch}",
        f"  Отличие от оригинала: {idea.difference_from_original}",
        *(f"  • {p}" for p in idea.key_points),
        f"Shorts: {an.short_form_angle}",
        f"Уверенность {an.confidence:.2f} · {an.model} · ${an.cost:.4f}",
    ]
    return "\n".join(lines)


@app.command()
def analyze(
    video_id: str,
    force: Annotated[bool, typer.Option("--force", help="Игнорировать кеш")] = False,
) -> None:
    """Анализ ролика LLM по id или ссылке (любой ролик; кешируется — повторно не анализирует)."""
    from radar.analyze.analyzer import AnalysisBudgetExceeded
    from radar.llm.base import LLMError
    from radar.youtube.client import QuotaExceededError
    from radar.youtube.quota import QuotaDeferred

    a = get_app()
    try:
        from radar.collect.adhoc import ensure_video

        video = ensure_video(a.youtube, a.db, video_id, current_time())
        an = a.analyzer().analyze(video.id, current_time(), force=force)
    except (
        ConfigError,
        LLMError,
        AnalysisBudgetExceeded,
        QuotaDeferred,
        QuotaExceededError,
        ValueError,
    ) as e:
        fail(str(e))
        return
    typer.echo(format_analysis(an))


# --- дайджест, tick, бот ----------------------------------------------------------


@app.command()
def digest(
    send: Annotated[
        bool, typer.Option("--send", help="Отправить в Telegram (если ещё не отправлен)")
    ] = False,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Пересобрать неотправленный дайджест")
    ] = False,
) -> None:
    """Дайджест за сегодня (по digest.timezone): топ аутлайеров по нишам."""
    from radar.digest.build import build_digest, send_digest
    from radar.digest.render import plain, render_digest

    a = get_app()
    now = current_time()
    if send:
        try:
            _print_result(
                send_digest(
                    a.db,
                    a.config,
                    a.notifier,
                    now,
                    llm=a.llm if a.has_llm() else None,
                    profile=a.profile,
                )
            )
        except ConfigError as e:
            fail(str(e))
        return
    d = build_digest(a.db, a.config, now, rebuild=rebuild)
    for m in render_digest(d):
        typer.echo(plain(m.text))
        typer.echo("")
    typer.echo(
        f"Статус: {'отправлен ' + str(d.sent_at) if d.sent_at else 'не отправлен (radar digest --send)'}"
    )


@app.command()
def tick() -> None:
    """Выполнить все задачи с наступившим сроком (для launchd, каждые 15 минут)."""
    from radar.tick import TickLocked, run_tick

    a = get_app()
    try:
        results = run_tick(a, current_time())
    except TickLocked as e:
        typer.echo(str(e))
        return
    if not results:
        typer.echo("Нечего делать: все задачи выполнены.")
    for r in results:
        _print_result(r)


@app.command()
def bot() -> None:
    """Запустить Telegram-бота (отдельный процесс, long polling)."""
    from radar.bot.main import run_bot

    try:
        run_bot(get_app())
    except ConfigError as e:
        fail(str(e))


@app.command()
def trends(
    days: Annotated[int, typer.Option("--days", help="Окно в днях")] = 7,
    llm: Annotated[
        bool, typer.Option("--llm", help="Добавить LLM-резюме (prompts/trends.md)")
    ] = False,
) -> None:
    """Растущие паттерны заголовков, форматы, длительности, время публикации по нишам.

    Пишет data/exports/content_hints.yaml и threshold_recommendations.yaml (по фидбэку «Не то»).
    """
    from radar.digest.render import plain
    from radar.trends import render_recommendations, render_trends_text, run_trends

    a = get_app()
    report, recs, summary = run_trends(a, current_time(), days, with_llm=llm)
    typer.echo(plain(render_trends_text(report, summary)))
    typer.echo("")
    typer.echo(plain(render_recommendations(recs)))
    typer.echo(
        f"\nЭкспорт: {a.settings.exports_dir}/content_hints.yaml, threshold_recommendations.yaml"
    )


# --- квота и диагностика --------------------------------------------------------


@app.command()
def quota() -> None:
    """Расход квоты YouTube API за текущие сутки (по тихоокеанскому времени)."""
    a = get_app()
    st = a.planner.status(current_time())
    typer.echo(
        f"Квота {st['date']} (PT): {st['used']}/{st['budget']} ед., осталось {st['remaining']}; "
        f"discovery {st['discovery_used']}/{st['discovery_reserve']} (резерв)"
    )
    if st["paused_until"]:
        typer.echo(f"⏸ Пауза после quotaExceeded до {st['paused_until']}")
    for method, purpose, calls, units in st["breakdown"]:  # type: ignore[union-attr]
        typer.echo(f"  {method:<22} {purpose:<28} вызовов {calls:<5} ед. {units}")
    from radar.bot.handlers import llm_spend_line

    typer.echo(llm_spend_line(a, current_time()))


@app.command()
def topics() -> None:
    """Темы, отмеченные кнопкой «В темы» (data/exports/topic_suggestions.yaml)."""
    a = get_app()
    items = a.db.list_topic_suggestions()
    if not items:
        typer.echo("Тем пока нет — нажимайте «📌 В темы» в дайджесте.")
    for i, t in enumerate(items, 1):
        typer.echo(f"{i}. {t.title}\n   {t.why}")
        for p in t.key_points:
            typer.echo(f"   • {p}")
        typer.echo(f"   источник: {', '.join('https://youtu.be/' + v for v in t.source_video_ids)}")


@app.command()
def doctor(
    online: Annotated[
        bool, typer.Option("--online", help="Реальный пинг LLM (тратит токены)")
    ] = False,
) -> None:
    """Проверка ключей, конфигов, БД, бюджета квоты и LLM."""
    from radar.config import resolve_config_file

    problems = 0
    try:
        a = get_app()
    except Exception as e:  # конфиг невалиден — это и есть диагноз
        fail(f"не удалось загрузить конфиг/БД: {e}")
        return
    s = a.settings

    def line(ok: bool | None, text: str) -> None:
        mark = {True: "✅", False: "❌", None: "⚠️ "}[ok]
        typer.echo(f"{mark} {text}")

    def secret_state(v: object) -> bool:
        return v is not None and bool(v.get_secret_value())  # type: ignore[attr-defined]

    if s.radar_fake:
        line(None, "RADAR_FAKE=1: YouTube/LLM/Telegram — фейки на фикстурах")
    line(
        secret_state(s.youtube_api_key) or None,
        f"YOUTUBE_API_KEY: {'задан' if secret_state(s.youtube_api_key) else 'нет'}",
    )
    line(
        secret_state(s.anthropic_api_key) or None,
        f"ANTHROPIC_API_KEY: {'задан' if secret_state(s.anthropic_api_key) else 'нет'}; модель {s.anthropic_model}",
    )
    line(
        secret_state(s.telegram_bot_token) or None,
        f"TELEGRAM_BOT_TOKEN: {'задан' if secret_state(s.telegram_bot_token) else 'нет'}",
    )
    line(bool(s.admin_ids) or None, f"ADMIN_IDS: {len(s.admin_ids)} шт.")

    for name in ("settings", "niches", "channel_profile"):
        path = resolve_config_file(s.radar_config_dir, name)
        if path is None:
            line(None, f"config/{name}.yaml: нет (используются значения по умолчанию)")
        else:
            line(not path.name.endswith(".example.yaml") or None, f"config: {path}")
    niches = a.db.list_niches()
    line(bool(niches) or None, f"Ниш: {len(niches)} (включено {sum(n.enabled for n in niches)})")
    line(a.profile is not None or None, "Профиль канала: " + ("загружен" if a.profile else "нет"))

    try:
        a.db.conn.execute("CREATE TABLE IF NOT EXISTS _doctor(x)")
        a.db.conn.execute("DROP TABLE _doctor")
        line(
            True,
            f"БД: {a.db.path}, схема v{a.db.schema_version}, каналов {len(a.db.list_channels())}",
        )
    except Exception as e:
        problems += 1
        line(False, f"БД недоступна на запись: {e}")

    st = a.planner.status(current_time())
    line(
        not st["paused_until"],
        f"Квота: {st['used']}/{st['budget']} ед. сегодня (PT), резерв discovery {st['discovery_reserve']}"
        + (f", пауза до {st['paused_until']}" if st["paused_until"] else ""),
    )

    last_tick = a.db.get_kv("last_tick_at")
    line(bool(last_tick) or None, f"Последний tick: {last_tick or 'ещё не было'}")
    for name, at, status in a.db.list_task_runs():
        if status != "ok":
            line(None, f"Задача {name}: {status} ({at.isoformat()})")

    if a.has_llm():
        if online:
            try:
                r = a.llm.complete(
                    system="Ответь одним словом.", prompt="ping", max_tokens=1024, effort="low"
                )
                line(True, f"LLM доступна: {r.model}, ${r.cost:.5f}")
            except Exception as e:
                problems += 1
                line(False, f"LLM недоступна: {e}")
        else:
            line(True, "LLM: ключ есть (реальный пинг: radar doctor --online)")
    else:
        line(None, "LLM: нет ключа — анализ отключён")

    if problems:
        raise typer.Exit(1)


def main() -> None:  # pragma: no cover
    try:
        app()
    except ConfigError as e:
        typer.echo(f"Ошибка конфигурации: {e}", err=True)
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
