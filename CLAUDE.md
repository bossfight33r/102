# CLAUDE.md — правила для агента

Проект: Outlier Radar (Python 3.12+, uv). Новая сессия начинает с чтения этого файла и `docs/STATUS.md`.

## Правила
1. Не читать целиком большие файлы, фикстуры, логи и БД — только head/tail/grep.
2. Не коммитить `data/`, `.env`, ключи и токены. Не выводить секреты.
3. Структуры между модулями — только через `src/radar/schemas.py` (конфиг — `config.py`).
4. Внешние сервисы — только через клиентов в `youtube/` и `llm/` (и `bot/notifier.py` для Telegram); в тестах — фейки. Никаких реальных сетевых запросов в тестах.
5. Каждый вызов YouTube API проходит через `youtube.client.YouTube` (QuotaPlanner.check → вызов → quota_ledger).
6. Статистика (`video_snapshots`) append-only (триггеры в БД), задачи идемпотентны.
7. Работающие модули не переписывать без необходимости.
8. Бот обслуживает только `ADMIN_IDS`.
9. После каждой фазы: `make lint` → `make test` → docs (`docs/STATUS.md`, `CHANGELOG.md`) → commit → push.
10. Новая сессия начинает с чтения `CLAUDE.md` и `docs/STATUS.md`.

## Команды
- `uv sync` — зависимости; `make lint`, `make test`, `make fmt`.
- `RADAR_FAKE=1 RADAR_NOW=2026-10-06T06:00:00+00:00 uv run radar tick` — прогон на фикстурах.
- Фикстуры YouTube: `tests/fixtures/*.json`, генератор `tests/fixtures/generate_fixtures.py`.

## Карта
- `app.py` — сборка зависимостей (App.build с подменой фейками); `cli.py` — команды `radar`.
- `youtube/` — протокол клиента, HTTP-клиент (+ скачивание превью), фейк, квота и прогноз квоты. `llm/` — провайдер (обычный + Batch API), фейки.
- `collect/` (watchlist, snapshots, discovery, adhoc — разбор любого ролика, mine — мой канал) → `score/` → `analyze/` (синхронно или пакетами) → `digest/` (сборка, рендер, архив) → `bot/` (handlers, notifier, keyboards, main + watchdog), `export/` (темы, CSV, YAML).
- `tick.py` — планировщик задач; `trends.py` — тренды, вопросы зрителей, рекомендации по порогам; `delivery.py` — лимит повторов доставки; `backup.py` — бэкап БД.
- БД: миграции — только добавлением версии в `db._MIGRATIONS`; соединение под RLock (потоки бота).
- ADR — `docs/decisions/`. Неоднозначность решается простейшим вариантом + короткий ADR.
