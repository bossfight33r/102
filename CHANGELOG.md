# Changelog

## [Фаза 1] Сбор
- Watchlist через uploads-плейлисты (`collect/watchlist.py`): первая страница (до 50 видео) даёт свежие ролики и историю для базлайна; seed-каналы ниш резолвятся один раз; подписчики обновляются раз в сутки.
- Снимки (`collect/snapshots.py`): videos.list батчами по 50; <14 дней — каждые 6 ч, до 60 дней — раз в сутки, старше — не собираем; прогресс сохраняется побатчево.
- Discovery (`collect/discovery.py`): search.list с publishedAfter/regionCode/relevanceLanguage, ротация запросов, лимит discovery_per_day, кандидаты по диапазону подписчиков, автоодобрение по конфигу.
- Отложенные задачи (QuotaDeferred) не теряют найденное дорогими вызовами.
- CLI: `radar poll [--force]`, `radar discover`.

## [Фаза 0] Foundation
- Структура проекта, pyproject (uv), Makefile (setup/test/lint/bot/tick/demo), `.env.example`.
- Конфиг: `Settings` (env, секреты как SecretStr), `settings/niches/channel_profile.example.yaml`.
- `schemas.py` — все контракты между модулями.
- SQLite-схема (`db.py`): append-only снимки (триггеры), quota_ledger, task_runs, alerts и др.
- Логирование structlog с маскировкой секретов.
- YouTube: протокол клиента, HTTP-клиент с ретраями, фейк на JSON-фикстурах, QuotaPlanner, обёртка с учётом каждого вызова.
- LLM: протокол, Anthropic (текст+изображения, учёт стоимости), FakeLLM.
- CLI: `radar niche add|list`, `radar channel add|list|approve|hide`, `radar quota`, `radar doctor`.
