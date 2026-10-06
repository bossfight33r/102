# Outlier Radar

Мониторит ниши на YouTube через **официальный YouTube Data API v3** (только API-ключ, публичные данные), находит ролики-аутлайеры — набравшие намного больше обычного для своего канала, — разбирает через Claude, почему они выстрелили, и каждое утро присылает в Telegram дайджест с идеями, **адаптированными под ваш канал** (паттерн, а не копия).

Без скрейпинга, yt-dlp, скачивания видео и субтитров: анализ строится на метаданных, главах, превью, динамике просмотров и топ-комментариях.

## Установка на Mac
```bash
brew install uv
git clone <repo> outlier-radar && cd outlier-radar
make setup                 # зависимости + .env и config/*.yaml из примеров
```
Заполните `.env` (`YOUTUBE_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`), где взять ключи — `docs/runbook.md`.

## Быстрый старт за 5 минут
1. `make demo` — весь конвейер на фикстурах без ключей и сети (дайджест печатается в консоль).
2. Опишите ниши в `config/niches.yaml` и свой канал в `config/channel_profile.yaml` (поля — `docs/niches.md`).
3. `uv run radar doctor` — ключи, конфиги, БД, квота, LLM.
4. `uv run radar poll && uv run radar discover` — первые каналы, видео и кандидаты.
5. `uv run radar tick` — скоринг, анализ, дайджест; затем launchd по `docs/runbook.md` (tick каждые 15 минут + бот).

## Команды
| Команда | Что делает |
|---|---|
| `radar niche add ID --name … -q запрос -c @канал` / `niche list` | ниши |
| `radar channel add @handle [-n ниша]` / `list [--status]` / `approve ID` / `hide ID` | каналы и кандидаты |
| `radar discover` | поиск кандидатов (search.list, в пределах `discovery_per_day`) |
| `radar poll [--force]` | новые видео watchlist + снимки статистики |
| `radar score` | базлайны и аутлайеры |
| `radar outliers [--niche ID] [--format short\|long] [--days N]` | список аутлайеров |
| `radar analyze VIDEO_ID\|ссылка [--force]` | разбор LLM любого ролика (кешируется) |
| `radar topics` | темы, отмеченные «В темы» |
| `radar digest [--send] [--rebuild]` | дайджест за сегодня / отправка в Telegram |
| `radar trends [--days 7] [--llm]` | растущие паттерны + `content_hints.yaml` + рекомендации по порогам |
| `radar quota [--plan]` | расход квоты YouTube и LLM; `--plan` — прогноз расхода в сутки при текущих настройках |
| `radar backup` | копия БД в `data/backups/` (tick делает раз в сутки) |
| `radar tick` | все задачи с наступившим сроком (для launchd) |
| `radar bot` | Telegram-бот: /digest /outliers /niches /candidates /quota /trends /analyze &lt;ссылка&gt; /add @канал; watchdog алертит, если tick встал |
| `radar doctor [--online]` | диагностика |

## Как это работает
`watchlist` (uploads-плейлисты) → `snapshots` (videos.list по 50) → `score` (медиана/MAD по формату, ratio, robust z, velocity) → `analyze` (Claude: метаданные + превью + комментарии + ваш профиль) → `digest` (Telegram, кнопки «В темы», «Подробнее», «Скрыть канал», «Не то») → `trends` (еженедельно).

Документация: `docs/architecture.md`, `docs/scoring.md`, `docs/quota.md`, `docs/runbook.md`, `docs/niches.md`, `docs/decisions/`. Статус разработки — `docs/STATUS.md`.

## Разработка
`make test`, `make lint`, `make fmt`. Тесты без сети: фейки YouTube (JSON-фикстуры), LLM и Telegram.
