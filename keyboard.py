import telebot

from tool import T, ikb, short, fmt_dt, user_tz

PAGE_SIZE = 8
TZ_BUTTONS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]  # часы


# ---- Reply-клавиатуры ----
def main_menu(user_id):
    b = T(user_id)["menu"]["buttons"]
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(b["create"], b["my"])
    kb.row(b["tz"], b["lang"])
    kb.row(b["support"], b["invite"])
    return kb


def back_kb(user_id):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(T(user_id)["common"]["back"])
    return kb


def media_kb(user_id):
    t = T(user_id)
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(t["draw"]["skip_media"])
    kb.row(t["common"]["back"])
    return kb


def start_kb(user_id):
    t = T(user_id)
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(t["draw"]["start_now_btn"])
    kb.row(t["common"]["back"])
    return kb


# ---- Кнопка под постом в канале ----
def join_kb(draw, count=0, preview=False):
    label = draw.button_text or "Участвовать"
    if count:
        label = f"{label} ({count})"
    return ikb([[(label, "noop" if preview else f"join:{draw.id}")]])


# ---- Редактор черновика ----
def draft_kb(user_id, d):
    b = T(user_id)["draw"]["editor"]
    i = d.id
    return ikb([
        [(b["start"], f"ed:{i}:start"), (b["end"], f"ed:{i}:end")],
        [(b["winners"], f"ed:{i}:winners"), (b["text"], f"ed:{i}:text")],
        [(b["media"], f"ed:{i}:media"), (b["button"], f"ed:{i}:btn")],
        [(b["subs"], f"subs:{i}"), (b["max"], f"ed:{i}:max")],
        [(b["publish"], f"pub:{i}")],
        [(b["delete"], f"cf:{i}:del")],
    ])


def subs_kb(user_id, d, subs):
    t = T(user_id)["subs"]
    rows = [[(f"❌ {short(s.title, 30)}", f"subdel:{d.id}:{s.id}")] for s in subs]
    rows.append([(t["add"], f"subadd:{d.id}")])
    rows.append([(t["done"], f"dr:{d.id}")])
    return ikb(rows)


def confirm_kb(user_id, draw_id, action):
    c = T(user_id)["common"]
    return ikb([[(c["yes"], f"do:{draw_id}:{action}"), (c["no"], f"v:{draw_id}")]])


# ---- Мои розыгрыши ----
def list_kb(user_id, draws, page, total):
    t = T(user_id)
    tz = user_tz(user_id)
    icons = t["status_icons"]
    rows = []
    for d in draws:
        label = f"{icons[d.status]} #{d.id} {short(d.channel_name, 18)} · {fmt_dt(d.end_at, tz)}"
        rows.append([(label, f"v:{d.id}")])
    nav = []
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if page > 0:
        nav.append(("◀️", f"lst:{page - 1}"))
    if page + 1 < pages:
        nav.append(("▶️", f"lst:{page + 1}"))
    if nav:
        rows.append(nav)
    return ikb(rows)


def card_kb(user_id, d, link=None):
    b = T(user_id)["my"]["btn"]
    i = d.id
    rows = []
    if d.status in ("draft", "scheduled"):
        rows.append([(b["edit"], f"ed:{i}:open"), (b["delete"], f"cf:{i}:del")])
    elif d.status == "active":
        rows.append([(b["players"], f"pl:{i}"), (b["finish"], f"cf:{i}:fin")])
    elif d.status == "finished":
        rows.append([(b["winners"], f"win:{i}"), (b["players"], f"pl:{i}")])
        rows.append([(b["reroll"], f"cf:{i}:rr"), (b["delete"], f"cf:{i}:del")])
    if link:
        rows.append([(b["link"], link)])
    rows.append([(b["back"], "lst:0")])
    return ikb(rows)


# ---- Часовой пояс ----
def tz_kb():
    rows, row = [], []
    for h in TZ_BUTTONS:
        row.append((f"UTC{'+' if h >= 0 else '-'}{abs(h)}", f"tz:{h * 60}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ikb(rows)
