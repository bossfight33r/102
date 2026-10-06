# 0007. Синхронный Notifier

**Решение.** `Notifier.send(list[OutMessage])` синхронный: tick — синхронный процесс. TelegramNotifier внутри делает `asyncio.run` с aiogram Bot и закрывает сессию. Бот (`radar bot`) — отдельный asyncio-процесс на aiogram Dispatcher.
**Последствия.** tick не зависит от event loop; отправка не блокирует бота.
