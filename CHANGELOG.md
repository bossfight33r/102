# Changelog

## [Фаза 0] Foundation
- Структура проекта, pyproject (uv), Makefile (setup/test/lint/bot/tick/demo), `.env.example`.
- Конфиг: `Settings` (env, секреты как SecretStr), `settings/niches/channel_profile.example.yaml`.
- `schemas.py` — все контракты между модулями.
- SQLite-схема (`db.py`): append-only снимки (триггеры), quota_ledger, task_runs, alerts и др.
- Логирование structlog с маскировкой секретов.
- YouTube: протокол клиента, HTTP-клиент с ретраями, фейк на JSON-фикстурах, QuotaPlanner, обёртка с учётом каждого вызова.
- LLM: протокол, Anthropic (текст+изображения, учёт стоимости), FakeLLM.
- CLI: `radar niche add|list`, `radar channel add|list|approve|hide`, `radar quota`, `radar doctor`.
