import json
import re
import traceback
from datetime import datetime, timedelta
from urllib.parse import quote

from telebot.apihelper import ApiTelegramException

import config
import keyboard
import middleware
import models
from app import bot, db, fsm
from models import utcnow
from tool import (BACK_LABELS, MENU_ACTIONS, MENU_LABELS, NOW_LABELS, SKIP_LABELS, T, bot_username,
                  ensure_user, esc, fmt_dt, get_user, html_text, ikb, is_users_admin, parse_local_time,
                  plain_len, short, tz_name, user_tz)


# ======================= ВАЛИДАТОРЫ ПОЛЕЙ =======================
# Каждый возвращает (значения_для_Draw, None) или (None, текст_ошибки).
# Одни и те же функции используются и при создании, и при редактировании.
def _int(m, lo, hi):
    if m.content_type != "text" or not re.fullmatch(r"\d{1,7}", (m.text or "").strip()):
        return None
    n = int(m.text.strip())
    return n if lo <= n <= hi else None


def v_text(m, t, ctx):
    if m.content_type != "text":
        return None, t["need_text"]
    long_ = ctx.get("file_type", "text") != "text"
    limit, n = (1024 if long_ else 4096), len(m.text)
    if n > limit:
        return None, t["caption_too_long" if long_ else "text_too_long"].format(n=n, limit=limit)
    return {"text": html_text(m)}, None  # html_text сохраняет жирный/курсив/ссылки


def _media_val(m):
    ct = m.content_type
    if ct == "photo":
        return "photo", m.photo[-1].file_id  # [-1] = максимальное качество
    if ct == "animation":
        return "animation", m.animation.file_id
    if ct == "video":
        return "video", m.video.file_id
    if ct == "document":
        return "document", m.document.file_id
    return None


def v_media(m, t, ctx):
    val = _media_val(m)
    if val is None:
        if m.content_type == "text" and m.text in SKIP_LABELS:
            val = ("text", "")
        else:
            return None, t["need_media"]
    n = plain_len(ctx.get("text"))
    if val[0] != "text" and n > 1024:
        return None, t["media_caption_long"].format(n=n)
    return {"file_type": val[0], "file_id": val[1]}, None


def v_winners(m, t, ctx):
    n = _int(m, 1, config.MAX_WINNERS)
    if n is None:
        return None, t["bad_range"].format(max=config.MAX_WINNERS)
    return {"winners_count": n}, None


def v_start(m, t, ctx):
    if m.content_type != "text":
        return None, t["invalid_format_time"]
    if m.text in NOW_LABELS:
        return {"start_at": None}, None
    dt = parse_local_time(m.text, ctx["tz"])
    if dt is None:
        return None, t["invalid_format_time"]
    if dt < utcnow() - timedelta(minutes=1):
        return None, t["over_time"]
    if ctx.get("end_at") and dt >= ctx["end_at"]:
        return None, t["end_before_start"]
    return {"start_at": dt}, None


def v_end(m, t, ctx):
    if m.content_type != "text":
        return None, t["invalid_format_time"]
    dt = parse_local_time(m.text, ctx["tz"])
    if dt is None:
        return None, t["invalid_format_time"]
    if dt <= utcnow():
        return None, t["over_time"]
    if ctx.get("start_at") and dt <= ctx["start_at"]:
        return None, t["end_before_start"]
    return {"end_at": dt}, None


def v_btn(m, t, ctx):
    s = (m.text or "").strip() if m.content_type == "text" else ""
    if not 1 <= len(s) <= 40:
        return None, t["button_too_long"]
    return {"button_text": s}, None


def v_max(m, t, ctx):
    n = _int(m, 0, 1_000_000)
    if n is None:
        return None, t["bad_max"]
    return {"max_players": n}, None


FIELDS = {
    "text": (v_text, "prompt_text"),
    "media": (v_media, "prompt_media"),
    "winners": (v_winners, "prompt_winners"),
    "start": (v_start, "prompt_start"),
    "end": (v_end, "prompt_end"),
    "btn": (v_btn, "prompt_btn"),
    "max": (v_max, "prompt_max"),
}
CREATE_FLOW = ["c_text", "c_media", "c_winners", "c_start", "c_end"]
FIELD_OF = {"c_text": "text", "c_media": "media", "c_winners": "winners", "c_start": "start", "c_end": "end"}


def ser(values):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in values.items()}


def make_ctx(uid, data):
    ctx = dict(data)
    for k in ("start_at", "end_at"):
        if ctx.get(k):
            ctx[k] = datetime.fromisoformat(ctx[k])
    ctx["tz"] = user_tz(uid)
    return ctx


def kb_for(uid, field, draw_id=None):
    if field == "media":
        return keyboard.media_kb(uid)
    quick = keyboard.quick_kb(uid, field, draw_id)
    if quick is not None:
        return quick
    return keyboard.back_kb(uid)


def ask(uid, field, draw_id=None):
    t = T(uid)["draw"]
    bot.send_message(uid, t[FIELDS[field][1]].format(tz=tz_name(user_tz(uid), uid), max=config.MAX_WINNERS),
                     reply_markup=kb_for(uid, field, draw_id), disable_web_page_preview=True)


# ======================= ОБЩЕЕ =======================
def answer(call, text=None, alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=alert)
    except Exception:
        pass


def edit_or_send(uid, message, text, kb):
    if message is not None:
        try:
            return bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=kb,
                                         disable_web_page_preview=True)
        except ApiTelegramException as e:
            if "not modified" in str(e):
                return
        except Exception:
            pass
    bot.send_message(uid, text, reply_markup=kb, disable_web_page_preview=True)


def send_menu(uid):
    bot.send_message(uid, T(uid)["menu"]["welcome_text"], reply_markup=keyboard.main_menu(uid))


def show_list(uid, page=0, message=None):
    t = T(uid)
    draws = db.select_all(models.Draw, order_by=models.Draw.id.desc(), owner_id=str(uid))
    if not draws:
        return edit_or_send(uid, message, t["my"]["no_draws"], None)
    pages = (len(draws) + keyboard.PAGE_SIZE - 1) // keyboard.PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = draws[page * keyboard.PAGE_SIZE:(page + 1) * keyboard.PAGE_SIZE]
    edit_or_send(uid, message, t["my"]["list_title"].format(page=page + 1, pages=pages),
                 keyboard.list_kb(uid, chunk, page, len(draws)))


def show_card(uid, d, message=None):
    link = middleware.post_link(d) if d.status in ("active", "finished") else None
    edit_or_send(uid, message, middleware.draw_summary(uid, d), keyboard.card_kb(uid, d, link))


# ======================= ПОЛЬЗОВАТЕЛИ БОТА (ТОЛЬКО ВЛАДЕЛЕЦ) =======================
USERS_PAGE_SIZE = 30
USERS_LABELS = {"пользователи", "пользватели", "пользователь", "users", "юзеры"}


def _users_command(m):
    if m.chat.type != "private" or m.content_type != "text":
        return False
    words = (m.text or "").strip().lower().split()
    if not words:
        return False
    if words[0].split("@")[0].lstrip("/").strip(".,!?") not in USERS_LABELS:
        return False
    user = m.from_user
    if not is_users_admin(user.id, getattr(user, "username", None)):
        return False
    state, _ = fsm.get_state(user.id)
    return state == "idle"


def _user_label(u):
    if u.username:
        return f"@{u.username}"
    name = (u.user_name or "").strip()
    return f"{name} · id {u.user_id}" if name else f"id {u.user_id}"


def _user_sort_key(u):
    try:
        return int(u.user_id)
    except (TypeError, ValueError):
        return 0


def show_users(uid, page=0, message=None):
    users = db.select_all(models.User)
    users.sort(key=_user_sort_key)
    if not users:
        return edit_or_send(uid, message, "👥 Пользователей пока нет.", None)
    pages = (len(users) + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    start = page * USERS_PAGE_SIZE
    chunk = users[start:start + USERS_PAGE_SIZE]
    lines = [f"👥 Всего пользователей: <b>{len(users)}</b>", f"Страница {page + 1}/{pages}", ""]
    lines += [f"{start + i + 1}. {esc(_user_label(u))}" for i, u in enumerate(chunk)]
    return edit_or_send(uid, message, "\n".join(lines), keyboard.users_kb(page, pages))


def _send_users_count(chat_id, user):
    ensure_user(user)
    total = db.count(models.User)
    pages = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    bot.send_message(chat_id, f"👥 Пользователей бота: <b>{total}</b>",
                     reply_markup=keyboard.users_kb(0, pages))


@bot.message_handler(func=_users_command)
def cmd_users(message):
    user = message.from_user
    if not is_users_admin(user.id, getattr(user, "username", None)):
        return
    _send_users_count(message.chat.id, user)


def m_users(m):
    user = m.from_user
    if not is_users_admin(user.id, getattr(user, "username", None)):
        return send_menu(m.chat.id)
    _send_users_count(m.chat.id, user)


# ======================= КАНАЛЫ =======================
def show_channels(uid, message=None):
    t = T(uid)["channels"]
    chats = middleware.available_channels(uid)
    edit_or_send(uid, message, t["title"] if chats else t["empty"],
                 keyboard.channels_kb(uid, chats, "create"))


def begin_create(uid, chat_id, call=None):
    t = T(uid)
    chat, err = middleware.verify_channel_for_user(uid, chat_id)
    if err:
        if call:
            return answer(call, t["draw"][err], True)
        return bot.send_message(uid, t["draw"][err], reply_markup=keyboard.main_menu(uid))
    if call:
        answer(call)
        _clear_buttons(uid, call)
    fsm.set_state(uid, "c_text", channel_id=str(chat.id), channel_name=chat.title or "",
                  channel_username=chat.username or "")
    ask(uid, "text")


def show_sub_channels(uid, draw_id, message=None):
    d = db.get_one(models.Draw, id=draw_id)
    if not d:
        return
    t = T(uid)["subs"]
    existing = {c.channel_id for c in db.select_all(models.DrawChannel, draw_id=d.id)}
    existing.add(str(d.channel_id))
    chats = [(c, ok) for c, ok in middleware.available_channels(uid) if c.chat_id not in existing]
    edit_or_send(uid, message, t["pick"] if chats else t["no_more"],
                 keyboard.channels_kb(uid, chats, "subs", d.id))


def notify_channel_added(chat, uid):
    t = T(uid)["channels"]
    try:
        bot.send_message(uid, t["added"].format(title=esc(chat.title or chat.id)),
                         reply_markup=ikb([[(t["create_btn"], f"ch:{chat.id}")]]),
                         disable_web_page_preview=True)
    except Exception:
        pass


def _clear_buttons(uid, call):
    try:
        bot.edit_message_reply_markup(uid, call.message.message_id, reply_markup=None)
    except Exception:
        pass


def _quick_values(field, p, start_at=None):
    if field == "winners":
        n = int(p[2])
        if not 1 <= n <= config.MAX_WINNERS:
            raise ValueError
        return {"winners_count": n}
    if field == "max":
        n = int(p[2])
        if not 0 <= n <= 1_000_000:
            raise ValueError
        return {"max_players": n}
    if field == "start":
        return {"start_at": None}
    minutes = int(p[2])
    if minutes <= 0:
        raise ValueError
    now = utcnow()
    base = start_at if (start_at and start_at > now) else now
    return {"end_at": base + timedelta(minutes=minutes)}


def quick_choice(call, cmd, p):
    uid = call.from_user.id
    t = T(uid)
    q = t["quick"]
    try:
        draw_id = int(p[1])
    except (IndexError, ValueError):
        return answer(call)
    if cmd in ("qwc", "qmc", "qsc", "qec"):
        hint = {"qwc": q["custom_winners"].format(max=config.MAX_WINNERS),
                "qmc": q["custom_max"],
                "qsc": q["custom_start"].format(tz=tz_name(user_tz(uid), uid)),
                "qec": q["custom_end"].format(tz=tz_name(user_tz(uid), uid))}[cmd]
        return answer(call, hint, True)
    field = {"qw": "winners", "qm": "max", "qs": "start", "qe": "end"}[cmd]
    if draw_id == 0:
        state, data = fsm.get_state(uid)
        if state != f"c_{field}":
            return answer(call, t["common"]["stale"], True)
        ctx = make_ctx(uid, data)
        try:
            values = _quick_values(field, p, ctx.get("start_at"))
        except (IndexError, ValueError):
            return answer(call)
        answer(call)
        _clear_buttons(uid, call)
        return advance_create(uid, data, field, values)

    d = owned(call, str(draw_id))
    if d is None:
        return
    if d.status != "draft":
        return answer(call, t["draw"]["not_editable"], True)
    try:
        values = _quick_values(field, p, d.start_at)
    except (IndexError, ValueError):
        return answer(call)
    if field == "end" and values["end_at"] <= utcnow():
        return answer(call, t["draw"]["over_time"], True)
    answer(call)
    _clear_buttons(uid, call)
    db.update(models.Draw, values, id=d.id)
    fsm.remove_state(uid)
    bot.send_message(uid, t["common"]["saved"], reply_markup=keyboard.main_menu(uid))
    middleware.show_draft(uid, d.id)


# ======================= КОМАНДЫ И МЕНЮ =======================
@bot.message_handler(commands=["start", "menu", "cancel"], func=lambda m: m.chat.type == "private")
def start(message):
    fsm.remove_state(message.chat.id)
    ensure_user(message.from_user)
    words = (message.text or "").split(maxsplit=1)
    command = (words[0] if words else "").split("@")[0].lower()
    payload = words[1].strip() if len(words) > 1 else ""
    if command == "/start" and payload.startswith("join_"):
        text, _ = middleware.begin_join(message.from_user, payload[5:])
        if text:
            bot.send_message(message.chat.id, text, reply_markup=keyboard.main_menu(message.chat.id),
                             disable_web_page_preview=True)
        return
    if command == "/start" and payload.startswith("ref_"):
        middleware.register_referral(message.from_user.id, payload[4:])
    send_menu(message.chat.id)
    if command == "/start" and not payload:
        pending = middleware.take_pending(message.from_user.id)
        if pending is not None:
            text, _ = middleware.begin_join(message.from_user, pending)
            if text:
                bot.send_message(message.chat.id, text, disable_web_page_preview=True)


def m_create(m):
    show_channels(m.chat.id)


def m_channels(m):
    show_channels(m.chat.id)


def m_my(m):
    show_list(m.chat.id, 0)


def m_lang(m):
    uid = m.chat.id
    user = get_user(uid)
    new = "ENG" if (user is None or user.language == "RU") else "RU"
    db.update(models.User, {"language": new}, user_id=str(uid))
    send_menu(uid)


def m_support(m):
    bot.send_message(m.chat.id, T(m.chat.id)["menu"]["support"])


def m_invite(m):
    uid = m.chat.id
    username = bot_username()
    link = f"https://t.me/{username}?start=ref_{uid}" if username else "https://t.me/your_bot"
    t = T(uid)["menu"]
    invited = middleware.referral_count(uid)
    share = ("https://t.me/share/url?url=" + quote(link, safe="")
             + "&text=" + quote(t["invite_share_text"], safe=""))
    bot.send_message(uid, t["invite"].format(link=link, invited=invited),
                     disable_web_page_preview=True,
                     reply_markup=ikb([[(t["invite_share"], share)]]))


MENU_HANDLERS = {"create": m_create, "my": m_my, "channels": m_channels,
                 "lang": m_lang, "support": m_support, "invite": m_invite, "users": m_users}


# ======================= АВТООБНАРУЖЕНИЕ КАНАЛОВ =======================
@bot.my_chat_member_handler()
def on_my_chat_member(update):
    try:
        chat = update.chat
        if chat.type == "private":
            return
        status = update.new_chat_member.status
        if status in ("left", "kicked"):
            middleware.forget_chat(chat.id)
            return
        can_post = bool(getattr(update.new_chat_member, "can_post_messages", False))
        user = getattr(update, "from_user", None)
        middleware.remember_chat(chat, getattr(user, "id", None), can_post=can_post)
        if status in ("administrator", "creator") and user and not user.is_bot:
            notify_channel_added(chat, user.id)
    except Exception:
        traceback.print_exc()


@bot.channel_post_handler(func=lambda m: True)
def on_channel_post(m):
    try:
        middleware.remember_chat(m.chat, None, can_post=True)
    except Exception:
        traceback.print_exc()


# ======================= ШАГИ (FSM) =======================
def _is_forward(m):
    return bool(getattr(m, "forward_origin", None) or getattr(m, "forward_from_chat", None))


def h_chat(m, t, data, state):
    uid = m.chat.id
    chat, err = middleware.resolve_chat(m, uid)
    if err:
        return bot.send_message(uid, t["draw"][err], reply_markup=keyboard.back_kb(uid))
    payload = {"channel_id": str(chat.id), "channel_name": chat.title or "",
               "channel_username": chat.username or ""}
    field = "text"
    if _is_forward(m):  # пересланный пост сразу становится текстом/медиа розыгрыша
        text = html_text(m)
        if text and plain_len(text) <= 4096:
            payload["text"] = text
            field = "media"
        media = _media_val(m)
        if media:
            if payload.get("text") and plain_len(payload["text"]) > 1024:
                field = "media"
            else:
                payload["file_type"], payload["file_id"] = media
                field = "winners" if payload.get("text") else "text"
    fsm.set_state(uid, f"c_{field}", **payload)
    ask(uid, field)


def advance_create(uid, data, field, values):
    data.update(ser(values))
    state = f"c_{field}"
    idx = CREATE_FLOW.index(state)
    if idx + 1 < len(CREATE_FLOW):
        nxt = CREATE_FLOW[idx + 1]
        fsm.set_state(uid, nxt, **data)
        return ask(uid, FIELD_OF[nxt])

    vals = dict(data)
    for k in ("start_at", "end_at"):
        vals[k] = datetime.fromisoformat(vals[k]) if vals.get(k) else None
    d = db.new(models.Draw, owner_id=str(uid), status="draft",
               button_text=T(uid)["post"]["join_default"], **vals)
    fsm.remove_state(uid)
    bot.send_message(uid, T(uid)["common"]["ok"], reply_markup=keyboard.main_menu(uid))
    middleware.show_draft(uid, d.id)


def h_create(m, t, data, state):
    uid = m.chat.id
    field = FIELD_OF[state]
    values, err = FIELDS[field][0](m, t["draw"], make_ctx(uid, data))
    if err:
        return bot.send_message(uid, err, reply_markup=kb_for(uid, field))
    advance_create(uid, data, field, values)


def h_edit(m, t, data, state):
    uid = m.chat.id
    d = db.get_one(models.Draw, id=data.get("draw_id"))
    if not d or d.owner_id != str(uid) or d.status != "draft":
        fsm.remove_state(uid)
        return send_menu(uid)
    field = data["field"]
    values, err = FIELDS[field][0](m, t["draw"], make_ctx(uid, {
        "file_type": d.file_type, "text": d.text, "start_at": d.start_at and d.start_at.isoformat(),
        "end_at": d.end_at and d.end_at.isoformat()}))
    if err:
        return bot.send_message(uid, err, reply_markup=kb_for(uid, field, d.id))
    db.update(models.Draw, values, id=d.id)
    fsm.remove_state(uid)
    bot.send_message(uid, t["common"]["saved"], reply_markup=keyboard.main_menu(uid))
    middleware.show_draft(uid, d.id, preview=field in ("text", "media", "btn"))


def h_sub_add(m, t, data, state):
    uid = m.chat.id
    d = db.get_one(models.Draw, id=data.get("draw_id"))
    if not d or d.owner_id != str(uid) or d.status != "draft":
        fsm.remove_state(uid)
        return send_menu(uid)
    chat, err = middleware.resolve_chat(m, uid, need_post=False)
    if err:
        return bot.send_message(uid, t["draw"][err], reply_markup=keyboard.back_kb(uid))
    subs = db.select_all(models.DrawChannel, draw_id=d.id)
    if str(chat.id) == d.channel_id or any(c.channel_id == str(chat.id) for c in subs):
        return bot.send_message(uid, t["subs"]["dup"], reply_markup=keyboard.back_kb(uid))
    if len(subs) >= config.MAX_SUB_CHANNELS:
        return bot.send_message(uid, t["subs"]["limit"].format(n=config.MAX_SUB_CHANNELS),
                                reply_markup=keyboard.back_kb(uid))
    db.new(models.DrawChannel, draw_id=d.id, channel_id=str(chat.id), title=chat.title or str(chat.id),
           link=f"https://t.me/{chat.username}" if chat.username else "")
    fsm.remove_state(uid)
    bot.send_message(uid, t["subs"]["added"], reply_markup=keyboard.main_menu(uid))
    middleware.show_subs(uid, d.id)


STATE_HANDLERS = {"c_chat": h_chat, "e_input": h_edit, "s_add": h_sub_add,
                  **{s: h_create for s in CREATE_FLOW}}


def go_back(uid, state, data):
    fsm.remove_state(uid)
    bot.send_message(uid, T(uid)["common"]["ok"], reply_markup=keyboard.main_menu(uid))
    draw_id = data.get("draw_id")
    if draw_id and state == "e_input":
        middleware.show_draft(uid, draw_id, preview=False)
    elif draw_id and state == "s_add":
        middleware.show_subs(uid, draw_id)


# ======================= РЕЗЕРВНАЯ КОПИЯ =======================
def _is_admin(user):
    username = (getattr(user, "username", "") or "").lower()
    return bool((username and username == config.ADMIN_USERNAME) or str(user.id) in config.ADMIN_IDS)


@bot.message_handler(commands=["backup"], func=lambda m: m.chat.type == "private")
def cmd_backup(message):
    if not _is_admin(message.from_user):
        return
    try:
        with open(db.path, "rb") as f:
            bot.send_document(message.chat.id, f, visible_file_name="bot_data.json",
                              caption="💾 Резервная копия данных")
    except Exception:
        traceback.print_exc()


def _restore_command(m):
    words = (m.caption or "").split()
    return bool(words) and words[0].split("@")[0] == "/restore"


@bot.message_handler(content_types=["document"],
                     func=lambda m: m.chat.type == "private" and _restore_command(m))
def cmd_restore(message):
    if not _is_admin(message.from_user):
        return
    try:
        info = bot.get_file(message.document.file_id)
        raw = json.loads(bot.download_file(info.file_path).decode("utf-8"))
        db.replace_all(raw)
        bot.send_message(message.chat.id, "✅ Данные восстановлены из резервной копии.")
    except Exception as e:
        traceback.print_exc()
        bot.send_message(message.chat.id,
                         f"⚠️ Не удалось восстановить данные: {esc(short(str(e), 200))}")


@bot.message_handler(content_types=["text", "photo", "document", "animation", "video"],
                     func=lambda m: m.chat.type == "private")
def router(m):
    try:
        uid = m.chat.id
        ensure_user(m.from_user)
        text = m.text if m.content_type == "text" else None

        if text in MENU_LABELS:
            fsm.remove_state(uid)
            return send_menu(uid)
        if text in MENU_ACTIONS:  # кнопка главного меню всегда прерывает текущий шаг
            fsm.remove_state(uid)
            return MENU_HANDLERS[MENU_ACTIONS[text]](m)

        state, data = fsm.get_state(uid)
        if state == "idle":
            return
        if text in BACK_LABELS:
            return go_back(uid, state, data)
        handler = STATE_HANDLERS.get(state)
        if handler is None:
            fsm.remove_state(uid)
            return send_menu(uid)
        handler(m, T(uid), data, state)
    except Exception:
        traceback.print_exc()
        try:
            bot.send_message(m.chat.id, T(m.chat.id)["common"]["error"])
        except Exception:
            pass


# ======================= CALLBACK-КНОПКИ =======================
def owned(call, raw_id):
    t = T(call.from_user.id, call.from_user.language_code)["common"]
    try:
        d = db.get_one(models.Draw, id=int(raw_id))
    except ValueError:
        d = None
    if not d:
        answer(call, t["not_found"], True)
    elif d.owner_id != str(call.from_user.id):
        answer(call, t["not_yours"], True)
    else:
        return d
    return None


def do_publish(uid, d, call):
    t = T(uid)
    now = utcnow()
    start = d.start_at if (d.start_at and d.start_at > now) else now
    if d.end_at is None or d.end_at <= now or start >= d.end_at:
        return answer(call, t["draw"]["over_time"], True)
    if not db.update(models.Draw, {"status": "scheduled", "start_at": start}, id=d.id, status="draft"):
        return answer(call, t["draw"]["not_editable"], True)
    answer(call)
    if start > now:
        return bot.send_message(uid, t["draw"]["scheduled"].format(
            start=fmt_dt(start, user_tz(uid)), tz=tz_name(user_tz(uid), uid)), reply_markup=keyboard.main_menu(uid))
    ok, res = middleware.publish_draw(d.id)
    if ok:
        text = t["draw"]["published"] + (f"\n{res}" if res else "")
        bot.send_message(uid, text, reply_markup=keyboard.main_menu(uid), disable_web_page_preview=True)


def do_action(uid, d, action, call):
    t = T(uid)
    if action == "del" and d.status != "active":
        middleware.delete_draw(d.id)
        answer(call, t["common"]["deleted"])
        return show_list(uid, 0, call.message)
    if action == "cancel" and d.status in ("scheduled", "active"):
        answer(call, t["common"]["cancelled"])
        middleware.cancel_draw(d.id)
        return show_card(uid, db.get_one(models.Draw, id=d.id), call.message)
    if action == "fin" and d.status == "active":
        answer(call, t["my"]["working"])
        middleware.finish_draw(d.id)
    elif action in ("rr", "rrx") and d.status == "finished":
        answer(call, t["my"]["working"])
        middleware.finish_draw(d.id, reroll=True, exclude_previous=(action == "rrx"))
    else:
        return answer(call, t["draw"]["not_editable"], True)
    show_card(uid, db.get_one(models.Draw, id=d.id), call.message)


def channel_reroll(call, raw_id):
    """Кнопка «Переиграть» под постом в канале — доступна только владельцу."""
    uid = call.from_user.id
    d = owned(call, raw_id)
    if d is None:
        return
    if d.status != "finished":
        return answer(call, T(uid)["draw"]["not_editable"], True)
    answer(call, T(uid)["my"]["working"])
    middleware.finish_draw(d.id, reroll=True)


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    try:
        route_callback(call)
    except Exception:
        traceback.print_exc()
        answer(call)


def route_callback(call):
    uid = call.from_user.id
    p = (call.data or "").split(":")
    cmd = p[0]

    if cmd == "noop":
        return answer(call)
    if cmd == "done":
        return answer(call, T(uid, call.from_user.language_code)["post"]["finished"], True)
    ensure_user(call.from_user)
    t = T(uid)

    if cmd == "join":
        text, alert = middleware.start_join(call)
        return answer(call, text or None, alert)
    if cmd == "cap":
        text, alert = middleware.handle_captcha(call)
        return answer(call, text or None, alert)

    if cmd == "usr":
        if not is_users_admin(uid, getattr(call.from_user, "username", None)):
            return answer(call)
        try:
            page = int(p[1])
        except (IndexError, ValueError):
            page = 0
        answer(call)
        return show_users(uid, page, call.message)

    if cmd == "lst":
        try:
            page = int(p[1])
        except (IndexError, ValueError):
            page = 0
        answer(call)
        return show_list(uid, page, call.message)
    if cmd == "chr":
        answer(call)
        return show_channels(uid, call.message)
    if cmd == "chm":
        answer(call)
        fsm.set_state(uid, "c_chat")
        return bot.send_message(uid, t["draw"]["prompt_chat"], reply_markup=keyboard.back_kb(uid),
                                disable_web_page_preview=True)
    if cmd == "chi":
        return answer(call, t["channels"]["how"], True)
    if cmd == "chn":
        return answer(call, t["channels"]["not_admin"], True)
    if cmd == "ch":
        return begin_create(uid, p[1] if len(p) > 1 else "", call)
    if cmd in ("qw", "qm", "qs", "qe", "qwc", "qmc", "qsc", "qec"):
        return quick_choice(call, cmd, p)
    if cmd == "rr":
        return channel_reroll(call, p[1] if len(p) > 1 else "")

    d = owned(call, p[1] if len(p) > 1 else "")
    if d is None:
        return

    if cmd == "chrs":
        answer(call)
        return show_sub_channels(uid, d.id, call.message)
    if cmd == "subm":
        answer(call)
        fsm.set_state(uid, "s_add", draw_id=d.id)
        return bot.send_message(uid, t["subs"]["prompt"], reply_markup=keyboard.back_kb(uid),
                                disable_web_page_preview=True)
    if cmd == "sub":
        if d.status != "draft":
            return answer(call, t["draw"]["not_editable"], True)
        chat_id = p[2] if len(p) > 2 else ""
        chat, err = middleware.verify_channel_for_user(uid, chat_id, need_post=False)
        if err:
            return answer(call, t["draw"][err], True)
        subs = db.select_all(models.DrawChannel, draw_id=d.id)
        if str(chat.id) == d.channel_id or any(c.channel_id == str(chat.id) for c in subs):
            return answer(call, t["subs"]["dup"], True)
        if len(subs) >= config.MAX_SUB_CHANNELS:
            return answer(call, t["subs"]["limit"].format(n=config.MAX_SUB_CHANNELS), True)
        db.new(models.DrawChannel, draw_id=d.id, channel_id=str(chat.id),
               title=chat.title or str(chat.id),
               link=f"https://t.me/{chat.username}" if chat.username else "")
        answer(call, t["subs"]["added"])
        return middleware.show_subs(uid, d.id, call.message)

    if cmd == "v":
        answer(call)
        return show_card(uid, d, call.message)
    if cmd == "dr":
        answer(call)
        return middleware.show_draft(uid, d.id, preview=False)
    if cmd == "pl":
        data, n = middleware.players_csv(d.id)
        if n == 0:
            return answer(call, t["my"]["no_players"], True)
        answer(call)
        return bot.send_document(uid, data, caption=t["my"]["players_caption"].format(id=d.id, n=n))
    if cmd == "win":
        winners = middleware.load_winners(d)
        answer(call)
        text = middleware.winners_text(winners) if winners else t["post"]["no_winners"]
        for part in middleware.split_text(text):
            bot.send_message(uid, part, disable_web_page_preview=True)
        return
    if cmd == "cf":
        action = p[2] if len(p) > 2 else ""
        ok = {"del": d.status != "active",
              "fin": d.status == "active",
              "rr": d.status == "finished",
              "rrx": d.status == "finished",
              "cancel": d.status in ("scheduled", "active")}.get(action)
        if not ok:
            return answer(call, t["draw"]["not_editable"], True)
        answer(call)
        return edit_or_send(uid, call.message, t["my"]["confirm"][action].format(id=d.id),
                            keyboard.confirm_kb(uid, d.id, action))
    if cmd == "do":
        if len(p) < 3:
            return answer(call)
        return do_action(uid, d, p[2], call)

    # ---- дальше только для черновиков ----
    if cmd == "ed" and len(p) > 2 and p[2] == "open":
        if d.status == "scheduled":
            db.update(models.Draw, {"status": "draft"}, id=d.id, status="scheduled")
        elif d.status != "draft":
            return answer(call, t["draw"]["not_editable"], True)
        answer(call)
        return middleware.show_draft(uid, d.id)
    if d.status != "draft":
        return answer(call, t["draw"]["not_editable"], True)

    if cmd == "ed" and len(p) > 2 and p[2] in FIELDS:
        answer(call)
        fsm.set_state(uid, "e_input", draw_id=d.id, field=p[2])
        return ask(uid, p[2], d.id)
    if cmd == "pub":
        return do_publish(uid, d, call)
    if cmd == "subs":
        answer(call)
        return middleware.show_subs(uid, d.id, call.message)
    if cmd == "subadd":
        if db.count(models.DrawChannel, draw_id=d.id) >= config.MAX_SUB_CHANNELS:
            return answer(call, t["subs"]["limit"].format(n=config.MAX_SUB_CHANNELS), True)
        answer(call)
        return show_sub_channels(uid, d.id, call.message)
    if cmd == "subdel":
        try:
            sub_id = int(p[2])
        except (IndexError, ValueError):
            return answer(call)
        db.delete(models.DrawChannel, id=sub_id, draw_id=d.id)
        answer(call)
        return middleware.show_subs(uid, d.id, call.message)
    answer(call)


if __name__ == "__main__":
    middleware.start_worker()
    print("Bot started")
    bot.infinity_polling(allowed_updates=[
        "message", "callback_query", "my_chat_member", "channel_post",
    ])
