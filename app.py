import atexit
import logging

import telebot

import config
from base import DataBase
from fsm import FSM

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO), format=LOG_FORMAT)
if config.LOG_FILE:
    try:
        handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)
    except OSError:
        logging.getLogger("bot").exception("не удалось открыть лог-файл %s", config.LOG_FILE)

if not config.token or ":" not in config.token:
    raise RuntimeError(
        "Bot token is not configured. Set the BOT_TOKEN environment variable "
        "to the token you received from @BotFather."
    )

# Тексты постов и сообщений используют HTML-разметку
bot = telebot.TeleBot(config.token, parse_mode="HTML", threaded=True, num_threads=8)

db = DataBase()
db.base_init()
atexit.register(db.save)
fsm = FSM(db)
