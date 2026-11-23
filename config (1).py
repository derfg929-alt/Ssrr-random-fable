import os

# Токен бота от @BotFather (лучше задавать через переменную окружения BOT_TOKEN)
token = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# База данных. Для начала хватит SQLite, для нагрузки можно postgresql://user:pass@host/db
db_url = os.getenv("DB_URL", "sqlite:///randomizer.db")

# Часовой пояс по умолчанию (в минутах от UTC). 180 = UTC+3 (Москва)
DEFAULT_TZ = 180

# Как часто воркер проверяет публикации/завершения/счётчики (сек)
WORKER_TICK = 3

# Как часто можно обновлять счётчик участников на кнопке поста (сек), чтобы не ловить флуд-лимиты
COUNTER_EDIT_INTERVAL = 10

# Лимиты
MAX_WINNERS = 500
MAX_SUB_CHANNELS = 5
