from datetime import datetime, timezone

from sqlalchemy import (Column, Integer, String, Text, DateTime, UniqueConstraint,
                        create_engine, event)
from sqlalchemy.orm import declarative_base

import config


def utcnow():
    """Наивное время в UTC. Всё время в БД хранится только в UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_is_sqlite = config.db_url.startswith("sqlite")
engine = create_engine(
    config.db_url,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    user_id = Column(String, primary_key=True)
    user_name = Column(String)
    language = Column(String, default="RU")
    tz_offset = Column(Integer, default=config.DEFAULT_TZ)  # минуты от UTC


class Draw(Base):
    """
    Один розыгрыш. Статусы:
    draft -> scheduled -> active -> finished   (+ cancelled)
    """
    __tablename__ = "draws"
    id = Column(Integer, primary_key=True)
    owner_id = Column(String, index=True)
    channel_id = Column(String)
    channel_name = Column(String)
    channel_username = Column(String)           # без @, если канал публичный
    text = Column(Text)                         # HTML
    file_type = Column(String, default="text")  # text/photo/animation/video/document
    file_id = Column(String, default="")
    winners_count = Column(Integer, default=1)
    max_players = Column(Integer, default=0)    # 0 = без лимита
    button_text = Column(String)                # None = текст по умолчанию
    start_at = Column(DateTime)                 # None = сразу при публикации
    end_at = Column(DateTime)
    status = Column(String, default="draft", index=True)
    message_id = Column(Integer)
    shown_count = Column(Integer, default=-1)   # какое число сейчас на кнопке
    winners = Column(Text, default="[]")        # JSON [{"id":..., "name":...}]
    created_at = Column(DateTime, default=utcnow)


class DrawChannel(Base):
    """Дополнительные каналы, на которые нужно подписаться для участия."""
    __tablename__ = "draw_channels"
    id = Column(Integer, primary_key=True)
    draw_id = Column(Integer, index=True)
    channel_id = Column(String)
    title = Column(String)
    link = Column(String)


class Player(Base):
    __tablename__ = "draw_players"
    id = Column(Integer, primary_key=True)
    draw_id = Column(Integer, index=True)
    user_id = Column(String)
    name = Column(String)
    username = Column(String)
    joined_at = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("draw_id", "user_id", name="uq_draw_user"),)


class UserState(Base):
    __tablename__ = "user_states"
    user_id = Column(String, primary_key=True)
    state = Column(String)
    data = Column(Text, default="{}")
