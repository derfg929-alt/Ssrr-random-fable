from datetime import datetime, timezone

import config


def utcnow():
    """Наивное время в UTC. Всё время в БД хранится только в UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Field:
    """Описание поля модели. На классе возвращает себя (для order_by), на объекте — значение."""

    def __init__(self, default=None):
        self.default = default
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if self.name in obj.__dict__:
            return obj.__dict__[self.name]
        return self.default() if callable(self.default) else self.default

    def __set__(self, obj, value):
        obj.__dict__[self.name] = value

    def desc(self):
        return OrderKey(self, True)

    def asc(self):
        return OrderKey(self, False)


class OrderKey:
    def __init__(self, field, reverse):
        self.field = field
        self.reverse = reverse


class Model:
    __tablename__ = ""
    __pk__ = ()
    __auto__ = None
    __unique__ = ()
    __datetime__ = ()

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self):
        pk = ", ".join(f"{k}={getattr(self, k)!r}" for k in self.__pk__)
        return f"<{type(self).__name__} {pk}>"


class User(Model):
    __tablename__ = "users"
    __pk__ = ("user_id",)
    user_id = Field()
    user_name = Field()
    language = Field("RU")
    tz_offset = Field(config.DEFAULT_TZ)  # минуты от UTC, оставлено для совместимости


class Draw(Model):
    """
    Один розыгрыш. Статусы:
    draft -> scheduled -> active -> finished   (+ cancelled)
    """
    __tablename__ = "draws"
    __pk__ = ("id",)
    __auto__ = "id"
    __datetime__ = ("start_at", "end_at", "created_at")
    id = Field()
    owner_id = Field()
    channel_id = Field()
    channel_name = Field()
    channel_username = Field()                  # без @, если канал публичный
    text = Field()                              # HTML
    file_type = Field("text")                   # text/photo/animation/video/document
    file_id = Field("")
    winners_count = Field(1)
    max_players = Field(0)                      # 0 = без лимита
    button_text = Field()                       # None = текст по умолчанию
    start_at = Field()                          # None = сразу при публикации
    end_at = Field()
    status = Field("draft")
    message_id = Field()
    shown_count = Field(-1)                     # какое число сейчас на кнопке
    winners = Field("[]")                       # JSON [{"id":..., "name":...}]
    created_at = Field(utcnow)


class DrawChannel(Model):
    """Дополнительные каналы, на которые нужно подписаться для участия."""
    __tablename__ = "draw_channels"
    __pk__ = ("id",)
    __auto__ = "id"
    id = Field()
    draw_id = Field()
    channel_id = Field()
    title = Field()
    link = Field()


class Player(Model):
    __tablename__ = "draw_players"
    __pk__ = ("id",)
    __auto__ = "id"
    __unique__ = (("draw_id", "user_id"),)
    __datetime__ = ("joined_at",)
    id = Field()
    draw_id = Field()
    user_id = Field()
    name = Field()
    username = Field()
    joined_at = Field(utcnow)


class UserState(Model):
    __tablename__ = "user_states"
    __pk__ = ("user_id",)
    user_id = Field()
    state = Field()
    data = Field("{}")


class BotChat(Model):
    """Каналы/группы, куда добавлен бот. Заполняется автоматически из updates."""
    __tablename__ = "bot_chats"
    __pk__ = ("chat_id",)
    __datetime__ = ("added_at", "seen_at")
    chat_id = Field()
    title = Field()
    username = Field()
    type = Field("channel")                     # channel / supergroup / group
    can_post = Field(0)
    added_by = Field()
    added_at = Field(utcnow)
    seen_at = Field(utcnow)


ALL_MODELS = (User, Draw, DrawChannel, Player, UserState, BotChat)
