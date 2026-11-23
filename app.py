import telebot

import config
from base import DataBase
from fsm import FSM

# Тексты постов и сообщений используют HTML-разметку
bot = telebot.TeleBot(config.token, parse_mode="HTML", threaded=True, num_threads=8)

db = DataBase()
db.base_init()
fsm = FSM(db)
