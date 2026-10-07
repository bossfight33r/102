# Конфиги

Файлы в `config/`. Если нет `X.yaml`, берётся `X.example.yaml` (`make setup` копирует примеры). Неизвестные ключи — ошибка (защита от опечаток).

## niches.yaml

```yaml
niches:
  - id: ai-tools                 # латиница/цифры/-/_, уникальный
    name: AI-инструменты         # для дайджеста
    language: ru                 # relevanceLanguage для search.list
    region: RU                   # regionCode для search.list
    seed_queries: [нейросети для работы, ai инструменты]
    seed_channels: ["@techguru", "UC..."]   # сразу в watchlist
    format: both                 # shorts | long | both — фильтр discovery и дайджеста
    min_subs: 3000               # кандидаты вне диапазона отбрасываются
    max_subs: 1000000
    discovery_per_day: 2         # вызовов search.list в сутки (по 100 ед.)
    enabled: true
```

Ниши синхронизируются в БД при каждом запуске (ADR 0002). `radar niche add` добавляет нишу только в БД.

## channel_profile.yaml — мой канал

```yaml
name: Мой канал
language: ru
topics: [автоматизация рутины с помощью AI]
audience_level: intermediate     # для кого снимаю
style: спокойный разбор, экран + голос
can_show: [запись экрана, свои проекты]   # что умею показывать
avoid: [лицо в кадре, реакции]             # чего не делаю
channel: "@my_handle"                      # необязательно: для radar me и /me
```

## settings.yaml

| Секция.поле | По умолчанию | Смысл |
|---|---|---|
| quota.daily_budget | 10000 | дневная квота |
| quota.discovery_reserve | 2000 | резерв под discovery |
| quota.safety_margin | 200 | неприкосновенный остаток для дорогих вызовов |
| quota.expensive_threshold | 50 | порог «дорогого» метода |
| quota.costs | см. docs/quota.md | стоимость методов |
| snapshots.fresh_days / fresh_interval_hours | 14 / 6 | частые снимки молодых видео |
| snapshots.mid_days / mid_interval_hours | 60 / 24 | редкие снимки; старше mid_days — не собираем |
| formats.short_max_sec | 180 | порог short/long |
| scoring.* | см. docs/scoring.md | базлайн, веса, пороги, флаги |
| analysis.score_threshold | 3.5 | анализировать аутлайеры со score не ниже |
| analysis.max_per_tick | 5 | анализов за один tick |
| analysis.comments_count | 20 | топ комментариев |
| analysis.max_cost_usd_per_day | 1.0 | дневной лимит расходов на LLM |
| analysis.use_thumbnails | true | отправлять превью как изображение |
| analysis.use_batch | false | анализ через Batch API (−50%, результат обычно < 1 ч) |
| analysis.batch_max_items / batch_timeout_hours | 20 / 25 | размер пакета; пакет старше — считается проваленным |
| llm.max_tokens | 16000 | лимит ответа анализа (включая мышление модели) |
| llm.light_effort / light_max_tokens | low / 4000 | для вступления дайджеста и резюме трендов |
| llm.input/output_usd_per_mtok | 4 / 20 | цены модели для учёта стоимости |
| llm.effort | medium | output_config.effort (null — не передавать) |
| llm.refusal_fallback | true | серверный fallback при отказе модели (не в Batch API) |
| llm.batch_discount | 0.5 | множитель стоимости пакетных запросов (только anthropic) |
| llm.json_mode / image_detail / reasoning_effort | true / null / null | для OpenAI-совместимых провайдеров, см. `docs/llm.md` |
| discovery.auto_approve | false | кандидаты сразу в watchlist |
| discovery.published_within_days | 30 | publishedAfter для search.list |
| discovery.max_results | 50 | результатов на запрос |
| watchlist.interval_minutes | 60 | частота опроса uploads-плейлистов |
| watchlist.channel_refresh_hours | 24 | обновление подписчиков |
| digest.timezone / send_hour | Europe/Moscow / 8 | когда отправлять дайджест |
| digest.top_n_per_niche / max_items | 5 / 15 | размер дайджеста |
| digest.lookback_hours | 48 | окно аутлайеров для дайджеста |
| digest.wait_analysis_hours | 2 | сколько ждать анализа после send_hour, прежде чем отправить как есть |
| digest.use_llm_intro | false | короткое вступление от LLM (prompts/digest.md) |
| trends.days | 7 | окно трендов |
| trends.weekly_weekday / weekly_hour | 0 / 10 | еженедельный отчёт (0 = пн, время по digest.timezone) |
| bot.watchdog_minutes / watchdog_check_minutes | 60 / 10 | алерт из бота, если tick не запускался дольше N минут (0 — выкл) |
| explore.days / max_topics | 30 / 5 | окно свежести и число тем за запуск (по ≈102 ед. квоты) |
| explore.small_channel_subs / big_channel_subs | 50000 / 500000 | границы «малого» и «крупного» канала |
| explore.examples / expand_count / popular_limit | 3 / 8 / 15 | примеров на нишу, подниш от LLM, строк в /popular |
| backup.enabled / keep | true / 7 | ежедневная копия БД в `data/backups/`, хранить N последних |
| trends.use_llm | false | LLM-резюме трендов (prompts/trends.md) |

## .env — секреты
`YOUTUBE_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS` (через запятую), необязательные `RADAR_DATA_DIR`, `RADAR_CONFIG_DIR`, `LOG_LEVEL`, `LOG_JSON`, `RADAR_FAKE`, `RADAR_NOW`.
