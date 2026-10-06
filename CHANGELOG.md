# Changelog

## [Надёжность]
- Промпты явно помечают заголовки, описания и комментарии YouTube как данные, а не инструкции (защита от prompt injection).
- Интеграционный тест: весь tick через настоящий `HttpYouTubeClient` на MockTransport с фикстурами — проверка путей, `part`, `forHandle`, `playlistId`, `regionCode`, лимита 50 id, учёта квоты и паузы после quotaExceeded.

## [Ревью кода]
- Дайджест, еженедельный отчёт и алерты не помечаются отправленными, если ничего не доставлено; повтор на следующем tick, не более 4 попыток (`delivery.py`), чтобы не долбить Telegram и не тратить LLM бесконечно.
- Длинные сообщения трендов обрезаются по целым строкам (не рвут HTML-теги и сущности).
- Стоимость оплаченного, но пустого ответа LLM (мышление съело max_tokens) учитывается в `llm_usage` и дневном лимите.
- Удалённые каналы больше не вызывают channels.list каждый tick.
- Миграции БД атомарны (скрипт + user_version в одной транзакции).
- `snapshots_by_video` фильтрует в SQL; тренды грузят видео один раз и токенизируют заголовки один раз.
- Скачивание превью перенесено в `youtube/api.py` (правило: внешние сервисы — только через клиентов).

## [Доработки 2]
- LLM: `llm.max_tokens` 16000 (мышление модели расходует лимит; пустой ответ при `max_tokens` → понятная ошибка); лёгкие задачи (вступление дайджеста, резюме трендов) — `effort: low`, `light_max_tokens`.
- Тренды: категория «Темы» (слова заголовков и теги с lift) — в отчёте, `/trends` и `content_hints.yaml` (`topics`).
- Discovery: известный канал, найденный по запросу другой ниши, получает и эту нишу.
- TelegramNotifier: инъекция aiogram-сессии, тест рассылки всем админам и фоллбэка «фото → текст».
- `make launchd-install|launchd-uninstall|launchd-status`, `make logs-rotate-install` (newsyslog).
- SessionStart-хук `.claude/hooks/session-start.sh`: в облачных сессиях Claude Code сразу `uv sync`.

## [Доработки] Эксплуатация
- БД v2: миграции по `user_version`; видео, которые API перестал возвращать (удалены/приватны), помечаются `gone_at` и больше не опрашиваются.
- Канал с пустым/удалённым uploads-плейлистом помечается опрошенным — не тратит квоту каждый tick.
- Дайджест пересобирается в момент отправки (предпросмотр `/digest` больше не «замораживает» его до анализа) и ждёт анализа до `digest.wait_analysis_hours` (2 ч).
- Аутлайеры скрытых каналов не анализируются (экономия LLM).
- Ошибка задачи tick → алерт в бот (раз в сутки на задачу, секреты вырезаются); `radar doctor` показывает последний tick и задачи не в статусе ok.
- CI: GitHub Actions (Python 3.12 и 3.13): ruff check/format, pytest.

## [Фаза 5] Тренды
- `trends.py`: lift признаков заголовков, форматов, длительностей, дня и времени публикации у аутлайеров против всех видео ниши, сравнение с прошлым окном.
- `radar trends [--days] [--llm]`, команда бота `/trends`, еженедельный отчёт в бот (tick-задача `trends_weekly`), `data/exports/content_hints.yaml`.
- Разбор фидбэка «Не то» → `data/exports/threshold_recommendations.yaml` (score_threshold, unreliable_factor, weight_velocity, min_views, формат ниши, скрытие канала); конфиг не меняется.
- README, runbook, STATUS финализированы; тесты запрещают реальный HTTP.

## [Фаза 4] Дайджест, бот, фидбэк, экспорт, tick, launchd
- `digest/`: топ-N свежих аутлайеров по нишам (фильтр формата ниши, скрытых каналов, «Не то», уже отправленных), карточки HTML с превью и кнопками, LLM-вступление (опционально), отправка один раз в сутки после `send_hour` по `timezone`.
- `bot/`: aiogram 3; middleware пропускает только ADMIN_IDS; команды /digest /outliers /niches /candidates /quota; кнопки «В темы», «Подробнее», «Скрыть канал», «Не то» → Feedback; одобрение кандидатов.
- `export/suggestions.py`: `data/exports/topic_suggestions.yaml` (атомарная запись, без дублей).
- `tick.py`: задачи с due-функциями, изоляция ошибок, блокировка параллельных запусков, `last_tick_at`; алерты (quotaExceeded) уходят в бот.
- `deploy/`: launchd-plist для tick (15 мин) и бота (KeepAlive); runbook.
- CLI: `radar digest [--send] [--rebuild]`, `radar tick`, `radar bot`. Демо-режим печатает сообщения в консоль.

## [Фаза 3] Анализ
- `analyze/analyzer.py`: метаданные, главы, динамика просмотров, превью (изображение), топ-20 комментариев, мой профиль → LLM → строгий `Analysis` (pydantic, extra=forbid), одна повторная попытка при невалидном ответе.
- Кеш по хешу стабильных входов, учёт стоимости (`llm_usage`, `Analysis.cost`), дневной лимит расходов, бэкофф для упавших.
- `analyze/thumbnails.py`: кеш превью в `data/cache/thumbs`, только *.ytimg.com. `analyze/comments.py`.
- Промпты `prompts/analyze.md` (адаптация паттерна, не копирование), `digest.md`, `trends.md`.
- CLI: `radar analyze VIDEO_ID [--force]`.

## [Фаза 2] Скоринг
- Базлайн канала по формату: медиана и MAD лог-просмотров последних N зрелых видео, leave-one-out, reliable при малой выборке.
- ratio, robust z (лог-шкала), velocity (по истории снимков или вогнутый prior), итоговый score с весами из конфига, порог min_views.
- reason_flags: high_ratio, high_z, fast_start, early_signal, views_exceed_subs, small_channel_breakout, low_confidence, velocity_fallback.
- CLI: `radar score`, `radar outliers [--niche] [--format] [--days]`.
- docs/scoring.md с формулами и разобранными примерами.

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
