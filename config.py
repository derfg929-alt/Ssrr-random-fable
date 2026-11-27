import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Читает .env без внешних зависимостей. Переменные окружения важнее файла."""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv(os.path.join(BASE_DIR, ".env"))

# Токен бота от @BotFather (задавайте через переменную окружения BOT_TOKEN)
token = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# Данные бота хранятся в JSON-снимке и журнале операций рядом с ним.
# DATA_DIR можно указать на постоянный диск хостинга, чтобы данные пережили обновление.
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data")))
DB_FILE = os.getenv("DB_FILE", "bot_data.json")
db_path = DB_FILE if os.path.isabs(DB_FILE) else os.path.join(DATA_DIR, DB_FILE)

# Журнал операций: сколько операций копить до перезаписи снимка и сбрасывать ли на диск.
JOURNAL_COMPACT_OPS = int(os.getenv("JOURNAL_COMPACT_OPS", "500"))
DB_FSYNC = os.getenv("DB_FSYNC", "1").lower() not in ("0", "false", "no", "off")

# Время всегда московское (МСК, UTC+3). 180 минут = UTC+3.
DEFAULT_TZ = 180

# Капча с картинками при нажатии «Участвовать» (1 — включена)
CAPTCHA_ENABLED = os.getenv("CAPTCHA_ENABLED", "1").lower() not in ("0", "false", "no", "off")

# Владелец бота: всегда имеет админ-доступ (даже без переменных окружения).
OWNER_IDS = {"8039111975"}

# Кому доступны команды /backup и /restore (резервная копия JSON).
# Пустое значение = доступ только владельцу и ADMIN_IDS.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").lstrip("@").lower()
ADMIN_IDS = OWNER_IDS | {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# Кому доступны «Статистика» и «Пользователи» (и админ-панель)
USERS_ADMIN_IDS = OWNER_IDS | {x.strip() for x in os.getenv("USERS_ADMIN_ID", "").split(",") if x.strip()}

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

# Напоминания владельцу о скором завершении розыгрыша (за 24 часа и за 1 час)
REMINDERS_ENABLED = os.getenv("REMINDERS_ENABLED", "1").lower() not in ("0", "false", "no", "off")

# Закреплять сообщение с итогами в канале (если у бота есть право закреплять)
PIN_RESULTS = os.getenv("PIN_RESULTS", "1").lower() not in ("0", "false", "no", "off")

# Логи: уровень и (необязательно) файл
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "")

# Лимиты
MAX_WINNERS = 500
MAX_SUB_CHANNELS = 5

# Билеты за дополнительные действия (включаются отдельно для каждого розыгрыша)
REF_BONUS_TICKETS = 1        # +1 билет за каждого приглашённого друга
BOOST_TICKETS_DEFAULT = 3    # +3 билета за один буст канала
MAX_REROLL_LOG = 20          # сколько раундов переигровок хранить в истории
