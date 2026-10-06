# Runbook

## Установка на Mac
```bash
brew install uv                 # если нет
git clone <repo> ~/outlier-radar && cd ~/outlier-radar
make setup                      # uv sync, .env и config/*.yaml из примеров, data/
$EDITOR .env                    # YOUTUBE_API_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_IDS
$EDITOR config/niches.yaml config/channel_profile.yaml
uv run radar doctor             # всё ли на месте
uv run radar doctor --online    # + реальный пинг LLM (копейки)
```
Ключи:
- **YouTube**: Google Cloud Console → проект → включить «YouTube Data API v3» → Credentials → API key (ограничьте ключ этим API).
- **Telegram**: @BotFather → /newbot → токен. Свой user id — у @userinfobot; запишите в `ADMIN_IDS`.
- **Anthropic**: console.anthropic.com → API keys. Модель — `ANTHROPIC_MODEL`.

## Первый прогон вручную
```bash
uv run radar poll          # seed-каналы → watchlist → снимки
uv run radar discover      # кандидаты (200 ед. квоты на нишу при discovery_per_day=2)
uv run radar channel list --status candidate
uv run radar channel approve UC...
uv run radar score && uv run radar outliers
uv run radar analyze VIDEO_ID
uv run radar digest        # предпросмотр; --send — в Telegram
uv run radar quota
```

## Демо без ключей
`make demo` — tick на фикстурах (`RADAR_FAKE=1`, данные в `data/demo`), сообщения печатаются в консоль.

## launchd: tick каждые 15 минут
Коротко: `make launchd-install` (tick + бот), `make launchd-status`, `make launchd-uninstall`; ротация логов — `make logs-rotate-install` (newsyslog, нужен sudo). Вручную:
```bash
mkdir -p data/logs
sed "s#__PROJECT_DIR__#$PWD#g" deploy/com.outlierradar.tick.plist > ~/Library/LaunchAgents/com.outlierradar.tick.plist
sed "s#__PROJECT_DIR__#$PWD#g" deploy/com.outlierradar.bot.plist  > ~/Library/LaunchAgents/com.outlierradar.bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.outlierradar.tick.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.outlierradar.bot.plist
launchctl print gui/$(id -u)/com.outlierradar.tick | head -20   # статус
tail -f data/logs/tick.err.log                                    # логи (structlog пишет в stderr)
```
Перезапуск после обновления кода: `launchctl kickstart -k gui/$(id -u)/com.outlierradar.bot`.
Остановка: `launchctl bootout gui/$(id -u)/com.outlierradar.tick` (и `.bot`).
plist запускает `zsh -lc`, чтобы подхватить PATH с `uv` (Homebrew). Если `uv` не находится — укажите полный путь (`which uv`).

## Что делает tick
Задачи по порядку, каждая — только если наступил срок: `seed_channels` → `watchlist` (интервал `watchlist.interval_minutes`) → `snapshots` → `discovery` (пока не исчерпан `discovery_per_day`) → `score` (если появились снимки) → `analyze` (есть неразобранные аутлайеры) → `digest` (после `digest.send_hour` по `digest.timezone`, раз в день) → `trends_weekly` → `alerts`. Ошибка одной задачи не останавливает остальные (`task_runs.status = error`) и приходит алертом в бот (раз в сутки на задачу). Дайджест ждёт анализа неразобранных аутлайеров до `digest.wait_analysis_hours` после `send_hour`. Параллельный запуск блокируется `data/tick.lock`. Время последнего запуска — `kv.last_tick_at`.

## Частые ошибки
| Симптом | Причина / решение |
|---|---|
| `Ошибка конфигурации: YOUTUBE_API_KEY не задан` | заполните `.env` (или `RADAR_FAKE=1` для демо) |
| в логах `quota_exceeded`, алерт в боте | дневная квота исчерпана; сбор на паузе до полуночи PT. Поднимите `watchlist.interval_minutes`, уменьшите `discovery_per_day` |
| задачи `deferred` в `radar tick` | планировщик бережёт квоту/резерв — это норма около лимита (`radar quota`) |
| `HTTP 403 accessNotConfigured` | API не включён в проекте Google Cloud |
| `HTTP 400 keyInvalid` | неверный ключ YouTube |
| `LLMError: Anthropic API: HTTP 401` | неверный `ANTHROPIC_API_KEY` |
| `лимит $…/сутки на LLM исчерпан` | `analysis.max_cost_usd_per_day` |
| бот молчит | ваш id не в `ADMIN_IDS` (чужим бот не отвечает), процесс не запущен (`launchctl print …bot`) |
| `radar tick уже выполняется` | предыдущий tick ещё идёт; если завис — `ps aux | grep radar` |
| дайджест не пришёл | `radar digest` — собран ли, `sent_at`; `radar doctor` — токен и ADMIN_IDS; логи `tick.err.log` |
| квота кончается к вечеру | `radar quota --plan` — прогноз по задачам; поднимите `watchlist.interval_minutes` |
| аутлайеров нет | мало истории: базлайну нужны видео старше 7 дней (`radar channel list`, `radar outliers --days 30`) |

## Бот
Команды: `/digest`, `/outliers`, `/niches`, `/candidates`, `/quota` (квота + расход LLM), `/trends`, `/questions` (вопросы зрителей за неделю), `/me` (мой канал против моей медианы, 3 ед. квоты), `/analyze <ссылка>` (любой ролик, 1–2 ед. квоты + LLM), `/add @канал [ниша]`. Watchdog внутри бота присылает алерт, если `radar tick` не запускался дольше `bot.watchdog_minutes` (Мак спал, launchd выгружен) — по одному на каждый простой.

## Данные
- БД: `data/radar.db` (SQLite, WAL). Бэкап — автоматически раз в сутки в `data/backups/radar-YYYYMMDD.db` (7 последних), вручную `radar backup`. Восстановление: остановить tick и бота (`make launchd-uninstall`), `cp data/backups/radar-…db data/radar.db`, `make launchd-install`.
- Вопросы зрителей: `data/exports/audience_questions.yaml`; аутлайеры в CSV: `radar export` → `data/exports/outliers.csv`.
- Архив отправленных дайджестов: `data/exports/digests/YYYY-MM-DD.md` (с полным анализом).
- Экспорт: `data/exports/topic_suggestions.yaml`, `content_hints.yaml`, `threshold_recommendations.yaml`.
- Кеш превью: `data/cache/thumbs` (можно удалить).
