"""CLI: radar <команда>."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Annotated

import typer

from radar.app import App, ConfigError, current_time
from radar.schemas import ChannelStatus, FormatPref, Niche

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

    if a.has_llm():
        if online:
            try:
                r = a.llm.complete(system="Ответь одним словом.", prompt="ping", max_tokens=16)
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
