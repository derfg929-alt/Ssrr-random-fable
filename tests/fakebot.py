"""Минимальная замена telebot.TeleBot для тестов: запоминает вызовы,
не ходит в сеть и позволяет подставить статусы участников/бусты."""

from types import SimpleNamespace


class FakeMessage:
    def __init__(self, message_id, chat_id, text=None):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text


class FakeBot:
    def __init__(self, token=None, **kwargs):
        self.token = token
        self.options = kwargs
        self.me = SimpleNamespace(id=42, username="testbot", is_bot=True)
        self.sent = []          # (chat_id, text, kwargs)
        self.photos = []        # (chat_id, caption, kwargs)
        self.edits = []         # (chat_id, message_id, text)
        self.markups = []       # (chat_id, message_id, markup)
        self.answers = []       # (text, alert)
        self.pins = []
        self.documents = []
        self.deleted = []
        self.commands = []
        self.chat_members = {}  # (chat_id, user_id) -> status
        self.chats = {}         # chat_id -> SimpleNamespace
        self.boosts = {}        # (chat_id, user_id) -> количество
        self.files = {}
        self.fail = {}          # имя метода -> исключение
        self._next_id = 100

    # ---- инфраструктура ----
    def reset(self):
        token = self.token
        self.__init__(token)

    def _check(self, name):
        error = self.fail.get(name)
        if error is not None:
            raise error

    def _new(self, chat_id, text=None):
        self._next_id += 1
        return FakeMessage(self._next_id, chat_id, text)

    # ---- регистрация хендлеров ----
    def message_handler(self, *args, **kwargs):
        return lambda fn: fn

    callback_query_handler = message_handler
    my_chat_member_handler = message_handler
    channel_post_handler = message_handler

    # ---- API ----
    def get_me(self):
        self._check("get_me")
        return self.me

    def send_message(self, chat_id, text, **kwargs):
        self._check("send_message")
        msg = self._new(chat_id, text)
        self.sent.append((chat_id, text, kwargs))
        return msg

    def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self._check("send_photo")
        self.photos.append((chat_id, caption, kwargs))
        return self._new(chat_id, caption)

    def send_document(self, chat_id, document, caption=None, **kwargs):
        self._check("send_document")
        self.documents.append((chat_id, document, caption))
        return self._new(chat_id, caption)

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self._check("edit_message_text")
        self.edits.append((chat_id, message_id, text))
        return FakeMessage(message_id, chat_id, text)

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self._check("edit_message_reply_markup")
        self.markups.append((chat_id, message_id, reply_markup))
        return True

    def edit_message_media(self, media, chat_id, message_id, reply_markup=None):
        self._check("edit_message_media")
        return True

    def delete_message(self, chat_id, message_id):
        self._check("delete_message")
        self.deleted.append((chat_id, message_id))
        return True

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return True

    def pin_chat_message(self, chat_id, message_id, **kwargs):
        self._check("pin_chat_message")
        self.pins.append((chat_id, message_id))
        return True

    def set_my_commands(self, commands):
        self.commands = list(commands)
        return True

    def get_chat(self, chat_id):
        self._check("get_chat")
        key = str(chat_id)
        if key in self.chats:
            return self.chats[key]
        return SimpleNamespace(id=chat_id, title="Test channel", username="testchan", type="channel")

    def get_chat_member(self, chat_id, user_id):
        self._check("get_chat_member")
        status = self.chat_members.get((str(chat_id), str(user_id)), "member")
        return SimpleNamespace(status=status, can_post_messages=True, is_member=status == "member")

    def get_user_chat_boosts(self, chat_id, user_id):
        self._check("get_user_chat_boosts")
        count = self.boosts.get((str(chat_id), str(user_id)), 0)
        return SimpleNamespace(boosts=[object()] * count)

    def get_file(self, file_id):
        return SimpleNamespace(file_path=file_id)

    def download_file(self, file_path):
        return self.files.get(file_path, b"{}")

    def stop_polling(self):
        return True

    def infinity_polling(self, **kwargs):
        return None
