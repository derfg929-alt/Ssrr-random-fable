import atexit

import telebot

import config
from base import DataBase
from fsm import FSM

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
