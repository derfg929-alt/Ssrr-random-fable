import os

# Токен бота от @BotFather (лучше задавать через переменную окружения BOT_TOKEN)
token = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# Данные бота хранятся в обычном JSON-файле и сохраняются автоматически.
# DATA_DIR можно указать на постоянный диск хостинга, чтобы данные пережили обновление.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data")))
DB_FILE = os.getenv("DB_FILE", "bot_data.json")
db_path = DB_FILE if os.path.isabs(DB_FILE) else os.path.join(DATA_DIR, DB_FILE)

# Время всегда московское (МСК, UTC+3). 180 минут = UTC+3.
DEFAULT_TZ = 180

# Добавлять в текст поста ссылку «Участвовать», чтобы она сохранялась при пересылке
JOIN_LINK_IN_POST = os.getenv("JOIN_LINK_IN_POST", "1").lower() not in ("0", "false", "no", "off")

# Кому доступны команды /backup и /restore (резервная копия JSON)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Som_teg").lstrip("@").lower()
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# Автоматически запоминать каналы, куда бот добавлен администратором
AUTO_DETECT_CHANNELS = os.getenv("AUTO_DETECT_CHANNELS", "1").lower() not in ("0", "false", "no", "off")

# Сколько каналов максимум показывать в списке выбора
MAX_LISTED_CHANNELS = int(os.getenv("MAX_LISTED_CHANNELS", "20"))

# Сколько секунд кэшировать проверку прав по каналам (чтобы не упереться в лимиты Telegram)
CHANNEL_CACHE_TTL = int(os.getenv("CHANNEL_CACHE_TTL", "20"))

# Как часто воркер проверяет публикации/завершения/счётчики (сек)
WORKER_TICK = 3

# Как часто можно обновлять счётчик участников на кнопке поста (сек), чтобы не ловить флуд-лимиты
COUNTER_EDIT_INTERVAL = 10

# Лимиты
MAX_WINNERS = 500
MAX_SUB_CHANNELS = 5
