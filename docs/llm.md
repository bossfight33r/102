# LLM для анализа

Анализ, вступление дайджеста и резюме трендов идут через один интерфейс `LLMProvider`. Модель меняется в `.env` без правки кода.

## Провайдеры

| `LLM_PROVIDER` | Адрес по умолчанию | Модель по умолчанию | Ключ |
|---|---|---|---|
| `gemini` (по умолчанию) | generativelanguage.googleapis.com/v1beta/openai | `gemini-3.5-flash-lite` | Google AI Studio → API key |
| `deepseek` | api.deepseek.com/v1 | `deepseek-flash` | platform.deepseek.com |
| `openai` | api.openai.com/v1 | `gpt-5-mini` | platform.openai.com |
| `openrouter` | openrouter.ai/api/v1 | `google/gemini-3.5-flash-lite` | openrouter.ai — один ключ на сотни моделей |
| `ollama` | localhost:11434/v1 | `qwen3-vl:8b` | не нужен |
| `custom` | `LLM_BASE_URL` | `LLM_MODEL` | `LLM_API_KEY` (если нужен) |
| `anthropic` | официальный SDK | `claude-haiku-4-5` | `ANTHROPIC_API_KEY` или `LLM_API_KEY` |

Все, кроме `anthropic`, — OpenAI-совместимый Chat Completions (`llm/openai_compat.py`, httpx). Модель должна понимать картинки (превью) — у всех моделей по умолчанию это так.

## Как попробовать модель
```bash
# в .env
LLM_PROVIDER=deepseek
LLM_MODEL=            # пусто — модель по умолчанию
LLM_API_KEY=...

uv run radar doctor --online                 # провайдер, модель, цена, реальный пинг
uv run radar analyze https://youtu.be/<id> --force   # разбор одного ролика на новой модели
uv run radar quota                            # расход LLM
```
Сравнить модели на одном ролике: меняете `LLM_PROVIDER`/`LLM_MODEL` и повторяете `radar analyze <ссылка> --force`. Кеш анализа учитывает модель — смена модели даёт новый разбор.

Ollama локально: `brew install ollama && ollama pull qwen3-vl:8b && ollama serve`, затем `LLM_PROVIDER=ollama`. Нужен Mac с 16+ ГБ памяти.

## Цены
Встроенная таблица `PRICES` в `llm/openai_compat.py` (ориентир по агрегаторам на октябрь 2026, $/1M вход/выход): gemini-3.5-flash-lite 0.30/2.50, deepseek-flash 0.14/0.28, gpt-5-mini 0.25/2.00, gpt-5-nano 0.05/0.40, ollama — 0. Неизвестная модель → цена 0, `radar doctor` предупреждает: задайте `llm.input_usd_per_mtok` и `llm.output_usd_per_mtok` в settings.yaml, иначе дневной лимит `analysis.max_cost_usd_per_day` не работает.

Ориентир за разбор (~4.5 тыс. токенов на вход с превью, ~1.2 тыс. на выход): Gemini 3.5 Flash-Lite ≈ $0.004, DeepSeek Flash ≈ $0.001, Ollama $0, Claude Haiku 4.5 ≈ $0.011.

## Настройки (settings.yaml → llm)
| Поле | По умолчанию | Смысл |
|---|---|---|
| `json_mode` | true | просить ответ JSON-объектом (`response_format`) |
| `image_detail` | null | `low` — дешевле картинка у OpenAI; null — не передавать |
| `reasoning_effort` | null | low/medium/high для «думающих» моделей (OpenAI, Gemini); null — не передавать |
| `input_usd_per_mtok` / `output_usd_per_mtok` | null | цены вручную вместо таблицы |
| `max_tokens` | 16000 | лимит ответа анализа |

Batch API (`analysis.use_batch`) пока только для `anthropic`; для остальных анализ идёт обычными запросами. Без LLM вообще: `analysis.enabled: false`.
