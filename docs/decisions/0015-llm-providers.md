# 0015. Любой LLM через OpenAI-совместимый API

**Контекст.** Claude для этой задачи дорог; хочется пробовать разные модели (Gemini, DeepSeek, OpenAI, локальные).
**Решение.** Второй `LLMProvider` — `OpenAICompatLLM` на httpx (Chat Completions), пресеты адресов и моделей по `LLM_PROVIDER`, таблица ориентировочных цен с переопределением в конфиге. По умолчанию `gemini` / `gemini-3.5-flash-lite`. Claude остаётся вариантом `LLM_PROVIDER=anthropic` (заменяет ADR 0005 как выбор по умолчанию). Анализ просит JSON через `response_format`, валидация та же (pydantic). Batch API — только у anthropic.
**Последствия.** Смена модели — правка `.env`; качество разборов нужно сравнить на реальных роликах (`radar analyze <ссылка> --force`).
