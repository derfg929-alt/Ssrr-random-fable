import telebot

from tool import T, ikb, short, fmt_dt, user_tz

PAGE_SIZE = 8
TZ_BUTTONS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]  # часы


# ---- Reply-клавиатуры ----
def main_menu(user_id):
    b = T(user_id)["menu"]["buttons"]
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(b["create"], b["my"])
    kb.row(b["channels"], b["tz"])
    kb.row(b["lang"], b["support"], b["invite"])
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


# ---- Каналы (автообнаружение) ----
def channels_kb(user_id, chats, mode="create", draw_id=None):
    """chats = [(BotChat, is_user_admin), ...]"""
    t = T(user_id)["channels"]
    rows = []
    for chat, is_admin in chats:
        title = short(chat.title or chat.chat_id, 40)
        if is_admin:
            data = f"ch:{chat.chat_id}" if mode == "create" else f"sub:{draw_id}:{chat.chat_id}"
            rows.append([(f"✅ {title}", data)])
        else:
            rows.append([(f"🔒 {title}", f"chn:{chat.chat_id}")])
    if mode == "create":
        rows.append([(t["refresh"], "chr"), (t["manual"], "chm")])
    else:
        rows.append([(t["refresh"], f"chrs:{draw_id}"), (t["manual"], f"subm:{draw_id}")])
    return ikb(rows)


# ---- Быстрые кнопки мастера ----
QUICK_WINNERS = (1, 2, 3, 5, 10, 25, 50, 100)
QUICK_MAX = (0, 10, 50, 100, 500, 1000)
QUICK_END = (("h1", 60), ("h6", 360), ("h12", 720),
             ("d1", 1440), ("d3", 4320), ("d7", 10080), ("d30", 43200))


def quick_kb(user_id, field, draw_id=None):
    t = T(user_id)["quick"]
    i = draw_id or 0
    if field == "winners":
        rows = [[(str(n), f"qw:{i}:{n}") for n in QUICK_WINNERS[j:j + 4]]
                for j in range(0, len(QUICK_WINNERS), 4)]
        rows.append([(t["custom"], f"qwc:{i}")])
    elif field == "max":
        rows = [[(t["no_limit"] if n == 0 else str(n), f"qm:{i}:{n}") for n in QUICK_MAX[j:j + 3]]
                for j in range(0, len(QUICK_MAX), 3)]
        rows.append([(t["custom"], f"qmc:{i}")])
    elif field == "start":
        rows = [[(t["now"], f"qs:{i}:now")], [(t["custom"], f"qsc:{i}")]]
    elif field == "end":
        rows = [[(t[key], f"qe:{i}:{minutes}") for key, minutes in QUICK_END[j:j + 3]]
                for j in range(0, len(QUICK_END), 3)]
        rows.append([(t["custom"], f"qec:{i}")])
    else:
        return None
    return ikb(rows)


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
def tz_kb(user_id):
    t = T(user_id)["tz"]
    rows, row = [[(t["msk"], "tz:180")]], []
    for h in TZ_BUTTONS:
        row.append((f"UTC{'+' if h >= 0 else '-'}{abs(h)}", f"tz:{h * 60}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ikb(rows)
