# STATUS

Новая сессия: прочитайте `CLAUDE.md` и этот файл, затем продолжайте с «Следующий шаг».

## Готово
- **Фаза 0 — Foundation**: структура, конфиг, схемы, БД, логирование с маскировкой секретов, YouTube-клиент (HTTP + фейк + квота), LLM (Anthropic + фейк), CLI-скелет, `radar doctor`, docs.

- **Фаза 1 — Сбор**: watchlist, снимки, discovery, кандидаты, QuotaPlanner в задачах; `radar poll`, `radar discover`.

- **Фаза 2 — Скоринг**: базлайны, ratio, z, velocity, score, reason_flags, раздельные форматы; `radar score`, `radar outliers`.

- **Фаза 3 — Анализ**: LLM с превью и комментариями, строгая схема Analysis, кеш, учёт стоимости; `radar analyze`.

## Не готово
- Фазы 4–5.

## Блокеры
- нет

## Проверить на Маке
```bash
make setup && uv run radar doctor
```

## Следующий шаг
Фаза 4 — дайджест, бот, фидбэк, экспорт, tick, launchd.
