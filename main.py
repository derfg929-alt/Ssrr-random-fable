import json
import re
import traceback
from datetime import datetime, timedelta

from telebot.apihelper import ApiTelegramException

import config
import keyboard
import middleware
import models
from app import bot, db, fsm
from models import utcnow
from tool import (BACK_LABELS, MENU_ACTIONS, MENU_LABELS, NOW_LABELS, SKIP_LABELS, T, ensure_user,
                  fmt_dt, get_user, parse_local_time, parse_tz, plain_len, tz_label, user_tz)


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
    return {"text": m.html_text}, None  # html_text сохраняет жирный/курсив/ссылки


def v_media(m, t, ctx):
    ct = m.content_type
    if ct == "photo":
        val = ("photo", m.photo[-1].file_id)  # [-1] = максимальное качество
    elif ct == "animation":
        val = ("animation", m.animation.file_id)
    elif ct == "video":
        val = ("video", m.video.file_id)
    elif ct == "document":
        val = ("document", m.document.file_id)
    elif ct == "text" and m.text in SKIP_LABELS:
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


def kb_for(uid, field):
    if field == "media":
        return keyboard.media_kb(uid)
    if field == "start":
        return keyboard.start_kb(uid)
    return keyboard.back_kb(uid)


def ask(uid, field):
    t = T(uid)["draw"]
    bot.send_message(uid, t[FIELDS[field][1]].format(tz=tz_label(user_tz(uid)), max=config.MAX_WINNERS),
                     reply_markup=kb_for(uid, field), disable_web_page_preview=True)


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


# ======================= КОМАНДЫ И МЕНЮ =======================
@bot.message_handler(commands=["start", "menu", "cancel"], func=lambda m: m.chat.type == "private")
def start(message):
    fsm.remove_state(message.chat.id)
    ensure_user(message.from_user)
    send_menu(message.chat.id)


def m_create(m):
    uid = m.chat.id
    fsm.set_state(uid, "c_chat")
    bot.send_message(uid, T(uid)["draw"]["prompt_chat"], reply_markup=keyboard.back_kb(uid),
                     disable_web_page_preview=True)


def m_my(m):
    show_list(m.chat.id, 0)


def m_tz(m):
    uid = m.chat.id
    tz = user_tz(uid)
    fsm.set_state(uid, "tz_input")
    bot.send_message(uid, T(uid)["tz"]["prompt"].format(tz=tz_label(tz), now=fmt_dt(utcnow(), tz)),
                     reply_markup=keyboard.tz_kb())


def m_lang(m):
    uid = m.chat.id
    user = get_user(uid)
    new = "ENG" if (user is None or user.language == "RU") else "RU"
    db.update(models.User, {"language": new}, user_id=str(uid))
    send_menu(uid)


def m_support(m):
    bot.send_message(m.chat.id, T(m.chat.id)["menu"]["support"])


def m_invite(m):
    bot.send_message(m.chat.id, T(m.chat.id)["menu"]["invite"])


MENU_HANDLERS = {"create": m_create, "my": m_my, "tz": m_tz, "lang": m_lang,
                 "support": m_support, "invite": m_invite}


# ======================= ШАГИ (FSM) =======================
def h_chat(m, t, data, state):
    uid = m.chat.id
    chat, err = middleware.resolve_chat(m, uid)
    if err:
        return bot.send_message(uid, t["draw"][err], reply_markup=keyboard.back_kb(uid))
    fsm.set_state(uid, "c_text", channel_id=str(chat.id), channel_name=chat.title or "",
                  channel_username=chat.username or "")
    ask(uid, "text")


def h_create(m, t, data, state):
    uid = m.chat.id
    field = FIELD_OF[state]
    values, err = FIELDS[field][0](m, t["draw"], make_ctx(uid, data))
    if err:
        return bot.send_message(uid, err, reply_markup=kb_for(uid, field))
    data.update(ser(values))
    idx = CREATE_FLOW.index(state)
    if idx + 1 < len(CREATE_FLOW):
        nxt = CREATE_FLOW[idx + 1]
        fsm.set_state(uid, nxt, **data)
        return ask(uid, FIELD_OF[nxt])

    vals = dict(data)
    for k in ("start_at", "end_at"):
        vals[k] = datetime.fromisoformat(vals[k]) if vals.get(k) else None
    d = db.new(models.Draw, owner_id=str(uid), status="draft",
               button_text=t["post"]["join_default"], **vals)
    fsm.remove_state(uid)
    bot.send_message(uid, t["common"]["ok"], reply_markup=keyboard.main_menu(uid))
    middleware.show_draft(uid, d.id)


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
        return bot.send_message(uid, err, reply_markup=kb_for(uid, field))
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


def h_tz(m, t, data, state):
    uid = m.chat.id
    minutes = parse_tz(m.text if m.content_type == "text" else "")
    if minutes is None:
        return bot.send_message(uid, t["tz"]["bad"])
    db.update(models.User, {"tz_offset": minutes}, user_id=str(uid))
    fsm.remove_state(uid)
    bot.send_message(uid, t["tz"]["saved"].format(tz=tz_label(minutes), now=fmt_dt(utcnow(), minutes)),
                     reply_markup=keyboard.main_menu(uid))


STATE_HANDLERS = {"c_chat": h_chat, "e_input": h_edit, "s_add": h_sub_add, "tz_input": h_tz,
                  **{s: h_create for s in CREATE_FLOW}}


def go_back(uid, state, data):
    fsm.remove_state(uid)
    bot.send_message(uid, T(uid)["common"]["ok"], reply_markup=keyboard.main_menu(uid))
    draw_id = data.get("draw_id")
    if draw_id and state == "e_input":
        middleware.show_draft(uid, draw_id, preview=False)
    elif draw_id and state == "s_add":
        middleware.show_subs(uid, draw_id)


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
            start=fmt_dt(start, user_tz(uid)), tz=tz_label(user_tz(uid))), reply_markup=keyboard.main_menu(uid))
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
    if action == "fin" and d.status == "active":
        answer(call, t["my"]["working"])
        middleware.finish_draw(d.id)
    elif action == "rr" and d.status == "finished":
        answer(call, t["my"]["working"])
        middleware.finish_draw(d.id, reroll=True)
    else:
        return answer(call, t["draw"]["not_editable"], True)
    show_card(uid, db.get_one(models.Draw, id=d.id), call.message)


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

    if cmd == "join":
        text, alert = middleware.join_draw(call)
        return answer(call, text, alert)
    if cmd == "noop":
        return answer(call)
    if cmd == "done":
        return answer(call, T(uid, call.from_user.language_code)["post"]["finished"], True)
    ensure_user(call.from_user)
    t = T(uid)

    if cmd == "tz":
        try:
            minutes = int(p[1])
        except (IndexError, ValueError):
            return answer(call)
        db.update(models.User, {"tz_offset": minutes}, user_id=str(uid))
        fsm.remove_state(uid)
        answer(call)
        return edit_or_send(uid, call.message, t["tz"]["saved"].format(
            tz=tz_label(minutes), now=fmt_dt(utcnow(), minutes)), None)
    if cmd == "lst":
        try:
            page = int(p[1])
        except (IndexError, ValueError):
            page = 0
        answer(call)
        return show_list(uid, page, call.message)

    d = owned(call, p[1] if len(p) > 1 else "")
    if d is None:
        return

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
        winners = json.loads(d.winners or "[]")
        answer(call)
        return bot.send_message(uid, middleware.winners_text(winners) if winners else t["post"]["no_winners"])
    if cmd == "cf":
        action = p[2] if len(p) > 2 else ""
        ok = {"del": d.status != "active", "fin": d.status == "active", "rr": d.status == "finished"}.get(action)
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
        return ask(uid, p[2])
    if cmd == "pub":
        return do_publish(uid, d, call)
    if cmd == "subs":
        answer(call)
        return middleware.show_subs(uid, d.id, call.message)
    if cmd == "subadd":
        if db.count(models.DrawChannel, draw_id=d.id) >= config.MAX_SUB_CHANNELS:
            return answer(call, t["subs"]["limit"].format(n=config.MAX_SUB_CHANNELS), True)
        answer(call)
        fsm.set_state(uid, "s_add", draw_id=d.id)
        return bot.send_message(uid, t["subs"]["prompt"], reply_markup=keyboard.back_kb(uid),
                                disable_web_page_preview=True)
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
    bot.infinity_polling(allowed_updates=["message", "callback_query"])
