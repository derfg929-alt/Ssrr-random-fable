import html
import json
import os
import re
from datetime import datetime, timedelta

import telebot

import config
import models
from app import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---- Тексты бота (RU.json / ENG.json) ----
def _load(name):
    with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


TEXTS = {"RU": _load("RU.json"), "ENG": _load("ENG.json")}
RU_CODES = {"ru", "uk", "be", "kk"}


def _labels(section, key):
    return {t[section][key] for t in TEXTS.values()}


# подписи кнопок на всех языках, чтобы не зависеть от языка пользователя
MENU_ACTIONS = {label: key for t in TEXTS.values() for key, label in t["menu"]["buttons"].items()}
BACK_LABELS = _labels("common", "back")
MENU_LABELS = _labels("common", "menu")
SKIP_LABELS = _labels("draw", "skip_media")
NOW_LABELS = _labels("draw", "start_now_btn")


# ---- Пользователи и язык ----
def get_user(user_id):
    return db.get_one(models.User, user_id=str(user_id))


def ensure_user(tg_user):
    user = get_user(tg_user.id)
    if user:
        return user
    code = (getattr(tg_user, "language_code", None) or "en").split("-")[0].lower()
    try:
        return db.new(models.User, user_id=str(tg_user.id),
                      user_name=tg_user.username or tg_user.first_name or "",
                      language="RU" if code in RU_CODES else "ENG",
                      tz_offset=config.DEFAULT_TZ)
    except Exception:  # параллельный /start
        return get_user(tg_user.id)


def lang_of(user_id, code=None):
    user = get_user(user_id)
    if user and user.language in TEXTS:
        return user.language
    if code is None:
        return "RU"
    return "RU" if code.split("-")[0].lower() in RU_CODES else "ENG"


def T(user_id, code=None):
    return TEXTS[lang_of(user_id, code)]


def user_tz(user_id):
    user = get_user(user_id)
    return user.tz_offset if user and user.tz_offset is not None else config.DEFAULT_TZ


# ---- Время ----
TIME_FORMATS = ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M")
TZ_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])?\s*(\d{1,2})(?::?(\d{2}))?$", re.I)


def parse_local_time(text, tz_minutes):
    """Строка во времени пользователя -> наивный UTC datetime (или None)."""
    text = (text or "").strip().replace(",", " ")
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=models.utcnow().year)
            if dt < models.utcnow() - timedelta(minutes=1):  # «31.12» без года = ближайший будущий год
                dt = dt.replace(year=dt.year + 1)
        return dt - timedelta(minutes=tz_minutes)
    return None


def fmt_dt(dt, tz_minutes):
    if not dt:
        return "—"
    return (dt + timedelta(minutes=tz_minutes)).strftime("%Y-%m-%d %H:%M")


def tz_label(minutes):
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def tz_name(minutes, uid=None):
    """Подпись часового пояса с человеческим названием для МСК."""
    if minutes == 180:
        return "МСК (UTC+3)" if (uid is None or lang_of(uid) == "RU") else "MSK (UTC+3)"
    return tz_label(minutes)


def parse_tz(text):
    raw = (text or "").strip()
    if raw.upper() in ("МСК", "MSK", "МОСКВА", "MOSCOW"):
        return 180
    m = TZ_RE.match(raw)
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    h, mm = int(m.group(2)), int(m.group(3) or 0)
    if mm >= 60:
        return None
    total = sign * (h * 60 + mm)
    return total if -12 * 60 <= total <= 14 * 60 else None


# ---- Текст ----
esc = html.escape


def html_text(m):
    """HTML-текст сообщения или подписи к медиа, с запасным вариантом."""
    try:
        if getattr(m, "text", None) is not None:
            return m.html_text or esc(m.text)
        if getattr(m, "caption", None) is not None:
            return getattr(m, "html_caption", None) or esc(m.caption)
    except Exception:
        pass
    return esc(getattr(m, "text", None) or getattr(m, "caption", None) or "")


def plain_len(html_text):
    """Длина текста без HTML-тегов (именно её считает Telegram для лимитов)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text or "")))


def short(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


# ---- Inline-клавиатуры ----
def ikb(rows):
    """rows = [[(текст, callback_data | https-ссылка), ...], ...]"""
    kb = telebot.types.InlineKeyboardMarkup()
    for row in rows:
        buttons = []
        for text, data in row:
            if data.startswith("http"):
                buttons.append(telebot.types.InlineKeyboardButton(text=text, url=data))
            else:
                buttons.append(telebot.types.InlineKeyboardButton(text=text, callback_data=data))
        kb.row(*buttons)
    return kb


def create_inlineKeyboard(key, row=0):
    """Совместимость со старым кодом: {текст: callback}."""
    row = row or len(key) or 1
    items = list(key.items())
    return ikb([items[i:i + row] for i in range(0, len(items), row)])
