# 0003. Правила QuotaPlanner

**Решение.**
- Сутки квоты — по America/Los_Angeles (так сбрасывает Google).
- Дешёвые вызовы не трогают ещё не потраченный резерв discovery: `used + units <= budget − (reserve − discovery_spent)`.
- Дорогие (стоимость ≥ `expensive_threshold`) и всё с purpose `discovery:*`: `used + units <= budget − safety_margin`.
- Отказ планировщика = `QuotaDeferred`: задача прерывается, `task_runs` не обновляется, повтор на следующем tick.
- `quotaExceeded` от API → запись в журнал, пауза до ближайшей полуночи PT (kv `quota_paused_until`), один алерт в сутки.
- Ошибочный вызов тоже списывает квоту (так считает Google).
