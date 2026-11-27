"""Общая настройка тестов: подменяем TeleBot до импорта app и чистим состояние."""

import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="bot-tests-")
os.environ["DB_FILE"] = "test.json"
os.environ["DB_FSYNC"] = "0"
os.environ["CAPTCHA_ENABLED"] = "1"
os.environ["ADMIN_IDS"] = ""
os.environ["ADMIN_USERNAME"] = ""
os.environ["USERS_ADMIN_ID"] = ""
os.environ["REMINDERS_ENABLED"] = "1"
os.environ["PIN_RESULTS"] = "1"

import telebot  # noqa: E402

from fakebot import FakeBot  # noqa: E402

telebot.TeleBot = FakeBot

import pytest  # noqa: E402

import captcha  # noqa: E402
import middleware  # noqa: E402
import models  # noqa: E402
from app import bot as _bot  # noqa: E402
from app import db as _db  # noqa: E402
from app import fsm as _fsm  # noqa: E402


@pytest.fixture
def bot():
    return _bot


@pytest.fixture
def db():
    return _db


@pytest.fixture
def fsm():
    return _fsm


@pytest.fixture(autouse=True)
def clean_state():
    _db.replace_all({model.__tablename__: [] for model in models.ALL_MODELS})
    middleware.invalidate_ref_counts()
    middleware._synced.clear()
    middleware._last_edit.clear()
    middleware._stop.clear()
    while not middleware._tasks.empty():
        middleware._tasks.get_nowait()
    captcha._items.clear()
    captcha._pending.clear()
    captcha._issued.clear()
    _bot.reset()
    yield


@pytest.fixture
def make_user():
    def _make(uid=555, username="user", first_name="Test", last_name=None, lang="ru"):
        return SimpleNamespace(id=uid, username=username, first_name=first_name,
                               last_name=last_name, language_code=lang, is_bot=False)
    return _make


@pytest.fixture
def make_message(make_user):
    def _make(text="hello", uid=555, chat_id=None, content_type="text", **kwargs):
        user = make_user(uid)
        chat = SimpleNamespace(id=chat_id if chat_id is not None else uid, type="private")
        return SimpleNamespace(
            chat=chat, from_user=user, content_type=content_type,
            text=text if content_type == "text" else None,
            caption=None if content_type == "text" else text,
            message_id=1, **kwargs)
    return _make


@pytest.fixture
def make_call(make_user):
    def _make(data, uid=555, username="user", chat_type="private", message_id=10):
        user = make_user(uid, username=username)
        return SimpleNamespace(
            id="callback-id", data=data, from_user=user,
            message=SimpleNamespace(message_id=message_id,
                                    chat=SimpleNamespace(id=uid, type=chat_type)))
    return _make
