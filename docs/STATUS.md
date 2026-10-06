# STATUS

Новая сессия: прочитайте `CLAUDE.md` и этот файл, затем продолжайте с «Следующий шаг».

## Готово (Фазы 0–5)
- **Фаза 0 — Foundation**: конфиг (env + YAML), `schemas.py`, SQLite (append-only снимки на триггерах), structlog с маскировкой секретов, YouTube-клиент (HTTP с ретраями + фейк на фикстурах + QuotaPlanner), LLM (Anthropic с изображениями + FakeLLM), CLI, `radar doctor`.
- **Фаза 1 — Сбор**: watchlist через uploads-плейлисты, снимки батчами по 50 по расписанию возраста, discovery в рамках `discovery_per_day` и бюджета, кандидаты, автоодобрение.
- **Фаза 2 — Скоринг**: базлайн (медиана/MAD лог-просмотров, leave-one-out, reliable), ratio, robust z, velocity (история / prior), score, reason_flags, раздельные форматы.
- **Фаза 3 — Анализ**: LLM с превью и топ-20 комментариев, строгая схема Analysis, кеш по хешу входа, учёт стоимости, дневной лимит.
- **Фаза 4 — Дайджест/бот/экспорт/tick**: дайджест по нишам, бот только для ADMIN_IDS, кнопки → Feedback, `topic_suggestions.yaml`, идемпотентный tick, launchd-plist для tick и бота.
- **Фаза 5 — Тренды**: `radar trends`, `/trends`, еженедельный отчёт в бот, `content_hints.yaml`, рекомендации по порогам из «Не то» в `threshold_recommendations.yaml`.

- **Доработки**: миграции БД, пропавшие видео, пустые плейлисты, дайджест ждёт анализ, алерты об ошибках tick, CI; лимиты LLM под мышление модели, темы в трендах, make-цели launchd, ротация логов, SessionStart-хук; исправления по ревью кода (доставка с лимитом попыток, атомарные миграции, учёт стоимости пустых ответов LLM).

Проверки: `make lint` — зелёный; `make test` — 122 теста (включая интеграционный прогон tick через настоящий HTTP-клиент на MockTransport) (Python 3.12 и 3.13), зелёные, без сети (реальный HTTP в тестах запрещён фикстурой в `tests/conftest.py`).

## Не готово
- Нет функциональных пробелов по ТЗ. Не проверено на реальных API (см. ниже).

## Блокеры
- нет

## Проверить на Маке
Окружение разработки без доступа к реальным YouTube/Anthropic/Telegram и без macOS — это проверяется вручную:
```bash
make setup && $EDITOR .env config/niches.yaml config/channel_profile.yaml
uv run radar doctor --online                     # ключи + реальный пинг LLM
uv run radar channel add @<известный_канал>      # реальный channels.list (forHandle), 1 ед.
uv run radar poll && uv run radar quota          # playlistItems/videos.list, учёт квоты
uv run radar discover                            # search.list: проверить regionCode/relevanceLanguage
uv run radar score && uv run radar outliers --days 30
uv run radar analyze <VIDEO_ID>                  # Claude с изображением превью, стоимость в выводе
uv run radar digest --send                       # карточки с превью и кнопками в Telegram
uv run radar bot                                 # нажать кнопки, /candidates, /trends; с чужого аккаунта — тишина
uv run radar trends --days 7 --llm
# launchd — по docs/runbook.md, затем:
launchctl print gui/$(id -u)/com.outlierradar.tick | grep -E "state|last exit"
tail -n 50 data/logs/tick.err.log
```
Что проверить глазами: серверный fallback Anthropic (`llm.refusal_fallback`) принимается выбранной моделью (если 400 — поставить `false`); `send_photo` по URL превью ytimg работает; цены `llm.*_usd_per_mtok` соответствуют модели.

## Следующий шаг
Прогнать «Проверить на Маке» на реальных ключах; по первым дням данных подстроить `scoring.*` (см. `threshold_recommendations.yaml` после отметок «Не то») и `watchlist.interval_minutes` под квоту.
