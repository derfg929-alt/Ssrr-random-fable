import csv
import io
import json
import random
import re
import threading
import time
import traceback

from telebot.apihelper import ApiTelegramException

import config
import keyboard
import models
from app import bot, db
from base import IntegrityError
from models import utcnow
from tool import T, esc, fmt_dt, ikb, plain_len, short, tz_name, user_tz

OK_STATUSES = ("member", "administrator", "creator")
_bot_id = None
_last_edit = {}          # draw_id -> время последнего обновления счётчика
_rng = random.SystemRandom()


def bot_id():
    global _bot_id
    if _bot_id is None:
        _bot_id = bot.get_me().id
    return _bot_id


# ======================= АВТООБНАРУЖЕНИЕ КАНАЛОВ =======================
_chat_cache = {}          # user_id -> (время, [(BotChat, is_admin), ...])


def remember_chat(chat, user_id=None, can_post=None):
    """Сохраняет канал/группу, куда добавлен бот, чтобы показывать её кнопкой."""
    if not config.AUTO_DETECT_CHANNELS or chat is None or chat.type == "private":
        return
    chat_id = str(chat.id)
    existing = db.get_one(models.BotChat, chat_id=chat_id)
    if existing and can_post is None and existing.seen_at \
            and (utcnow() - existing.seen_at).total_seconds() < 60:
        return  # частые посты в канале не должны писать в БД на каждое сообщение
    fields = {
        "title": chat.title or "",
        "username": chat.username or "",
        "type": chat.type,
        "seen_at": utcnow(),
    }
    if can_post is not None:
        fields["can_post"] = 1 if can_post else 0
    if existing:
        db.update(models.BotChat, fields, chat_id=chat_id)
    else:
        try:
            db.new(models.BotChat, chat_id=chat_id, added_by=str(user_id or ""), **fields)
        except Exception:  # параллельный update уже вставил строку
            db.update(models.BotChat, fields, chat_id=chat_id)
    _chat_cache.clear()


def forget_chat(chat_id):
    db.delete(models.BotChat, chat_id=str(chat_id))
    _chat_cache.clear()


def _as_chat_ref(chat_id):
    s = str(chat_id)
    return int(s) if re.fullmatch(r"-?\d+", s) else s


def _check_chat_access(chat, user_id, need_post=True):
    if chat.type == "private":
        return None, "bad_chat"
    try:
        um = bot.get_chat_member(chat.id, user_id)
    except Exception:
        return None, "not_admin"
    if um.status not in ("creator", "administrator"):
        return None, "not_admin"
    try:
        bm = bot.get_chat_member(chat.id, bot_id())
    except Exception:
        return None, "bot_not_admin"
    if bm.status not in ("creator", "administrator"):
        return None, "bot_not_admin"
    can_post = getattr(bm, "can_post_messages", False) or bm.status == "creator"
    if need_post and chat.type == "channel" and not can_post:
        return None, "bot_cant_post"
    remember_chat(chat, user_id, can_post=can_post)
    return chat, None


def verify_channel_for_user(user_id, chat_id, need_post=True):
    """Проверяет права пользователя и бота. Возвращает (chat, None) или (None, ключ_ошибки)."""
    try:
        chat = bot.get_chat(_as_chat_ref(chat_id))
    except Exception:
        return None, "chat_not_found"
    return _check_chat_access(chat, user_id, need_post)


def available_channels(user_id):
    """Каналы, где бот администратор. Возвращает [(BotChat, пользователь_админ)]."""
    key = str(user_id)
    cached = _chat_cache.get(key)
    if cached and time.time() - cached[0] < config.CHANNEL_CACHE_TTL:
        return cached[1]
    result = []
    for c in db.select_all(models.BotChat, order_by=models.BotChat.title):
        if len(result) >= config.MAX_LISTED_CHANNELS:
            break
        try:
            bm = bot.get_chat_member(c.chat_id, bot_id())
        except Exception:
            continue
        if bm.status not in ("creator", "administrator"):
            db.delete(models.BotChat, chat_id=c.chat_id)
            continue
        if c.type == "channel" and bm.status == "administrator" \
                and not getattr(bm, "can_post_messages", False):
            continue
        try:
            um = bot.get_chat_member(c.chat_id, user_id)
            is_admin = um.status in ("creator", "administrator")
        except Exception:
            is_admin = False
        result.append((c, is_admin))
    _chat_cache[key] = (time.time(), result)
    return result


# ======================= КАНАЛЫ / ЧАТЫ =======================
def extract_chat_ref(message):
    """Достаёт канал из пересланного поста, @username, ссылки t.me/... или числового id."""
    chat = getattr(message, "forward_from_chat", None)
    if chat is None:
        origin = getattr(message, "forward_origin", None)
        chat = getattr(origin, "chat", None) if origin else None
    if chat is not None:
        return chat.id
    text = (message.text or "").strip()
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", text)
    if m:
        return "@" + m.group(1)
    if re.match(r"^@[A-Za-z0-9_]{4,}$", text):
        return text
    if re.match(r"^-\d{5,}$", text):
        return int(text)
    return None


def resolve_chat(message, user_id, need_post=True):
    """Возвращает (chat, None) или (None, ключ_ошибки из texts['draw'])."""
    ref = extract_chat_ref(message)
    if ref is None:
        return None, "chat_not_found"
    try:
        chat = bot.get_chat(ref)
    except Exception:
        return None, "chat_not_found"
    return _check_chat_access(chat, user_id, need_post)


def post_link(d):
    if not d.message_id:
        return None
    if d.channel_username:
        return f"https://t.me/{d.channel_username}/{d.message_id}"
    cid = str(d.channel_id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{d.message_id}"
    return None


def send_post(chat_id, file_type, file_id, text, markup=None, reply_to=None):
    kw = {"reply_markup": markup}
    if reply_to:
        kw["reply_to_message_id"] = reply_to
    if file_type == "photo":
        return bot.send_photo(chat_id, file_id, caption=text, **kw)
    if file_type == "animation":
        return bot.send_animation(chat_id, file_id, caption=text, **kw)
    if file_type == "video":
        return bot.send_video(chat_id, file_id, caption=text, **kw)
    if file_type == "document":
        return bot.send_document(chat_id, file_id, caption=text, **kw)
    return bot.send_message(chat_id, text, **kw)


# ======================= ЧЕРНОВИК / КАРТОЧКА =======================
def draw_summary(uid, d):
    t = T(uid)
    s = t["draw"]["summary"]
    tz = user_tz(uid)
    subs = db.select_all(models.DrawChannel, draw_id=d.id)
    lines = [
        s["title"].format(id=d.id, status=f"{t['status_icons'][d.status]} {t['status'][d.status]}"),
        "",
        f"<b>{s['channel']}:</b> {esc(d.channel_name or '')}",
        f"<b>{s['start']}:</b> {fmt_dt(d.start_at, tz) if d.start_at else s['on_publish']}",
        f"<b>{s['end']}:</b> {fmt_dt(d.end_at, tz)}",
        f"<b>{s['tz']}:</b> {tz_name(tz, uid)}",
        f"<b>{s['winners']}:</b> {d.winners_count}",
        f"<b>{s['max']}:</b> {d.max_players or t['common']['unlimited']}",
        f"<b>{s['button']}:</b> {esc(d.button_text or '')}",
        f"<b>{s['subs']}:</b> " + (", ".join(esc(c.title or "") for c in subs) or t["common"]["none"]),
    ]
    if d.status in ("active", "finished"):
        lines.append(f"<b>{s['players']}:</b> {db.count(models.Player, draw_id=d.id)}")
    return "\n".join(lines)


def show_draft(uid, draw_id, preview=True):
    d = db.get_one(models.Draw, id=draw_id)
    if not d:
        return
    if preview:
        try:
            send_post(uid, d.file_type, d.file_id, d.text, keyboard.join_kb(d, 0, preview=True))
        except Exception as e:
            bot.send_message(uid, T(uid)["draw"]["preview_failed"].format(err=esc(short(str(e), 200))))
    bot.send_message(uid, draw_summary(uid, d), reply_markup=keyboard.draft_kb(uid, d))


def show_subs(uid, draw_id, message=None):
    d = db.get_one(models.Draw, id=draw_id)
    if not d:
        return
    t = T(uid)["subs"]
    subs = db.select_all(models.DrawChannel, order_by=models.DrawChannel.id, draw_id=d.id)
    lst = "\n".join(
        f"• <a href=\"{c.link}\">{esc(c.title)}</a>" if c.link else f"• {esc(c.title)}" for c in subs
    ) or t["empty"]
    text = t["title"].format(id=d.id, main=esc(d.channel_name or ""), list=lst, limit=config.MAX_SUB_CHANNELS)
    kb = keyboard.subs_kb(uid, d, subs)
    if message is not None:
        try:
            return bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=kb,
                                         disable_web_page_preview=True)
        except Exception:
            pass
    bot.send_message(uid, text, reply_markup=kb, disable_web_page_preview=True)


# ======================= УЧАСТИЕ =======================
def is_subscribed(chat_id, user_id):
    m = bot.get_chat_member(chat_id, user_id)
    return m.status in OK_STATUSES or (m.status == "restricted" and getattr(m, "is_member", False))


def missing_subscriptions(d, user_id):
    """(список каналов, на которые не подписан; ошибка_проверки)"""
    subs = db.select_all(models.DrawChannel, draw_id=d.id)
    targets = [(d.channel_id, d.channel_name)] + [(c.channel_id, c.title) for c in subs]
    missing = []
    for cid, title in targets:
        try:
            if not is_subscribed(cid, user_id):
                missing.append(title or str(cid))
        except Exception:
            return [], True
    return missing, False


def join_link(draw_id):
    """Ссылка на бота, которая сразу засчитывает участие (переживает пересылку поста)."""
    try:
        username = bot.get_me().username
    except Exception:
        username = ""
    return f"https://t.me/{username}?start=join_{draw_id}" if username else None


def join_draw(call):
    """Нажатие «Участвовать». Возвращает (текст, show_alert) для answer_callback_query."""
    user = call.from_user
    try:
        draw_id = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        return T(user.id, user.language_code)["post"]["finished"], True
    return join_draw_by_id(user, draw_id)


def join_draw_by_id(user, draw_id):
    """Участие по id — из кнопки или из диплинка /start join_<id>."""
    t = T(user.id, user.language_code)["post"]
    try:
        draw_id = int(draw_id)
    except (TypeError, ValueError):
        return t["finished"], True
    d = db.get_one(models.Draw, id=draw_id)
    if not d or d.status != "active":
        return t["finished"], True
    if db.get_one(models.Player, draw_id=d.id, user_id=str(user.id)):
        return t["already"], False
    if d.max_players and db.count(models.Player, draw_id=d.id) >= d.max_players:
        return t["full"], True

    missing, err = missing_subscriptions(d, user.id)
    if err:
        return t["check_failed"], True
    if missing:
        return short(t["not_subscribed"].format(channels=", ".join(missing)), 200), True

    name = " ".join(x for x in (user.first_name, user.last_name) if x) or user.username or str(user.id)
    try:
        db.new(models.Player, draw_id=d.id, user_id=str(user.id), name=short(name, 64), username=user.username)
    except IntegrityError:
        return t["already"], False

    # лимит достигнут -> воркер завершит розыгрыш на ближайшем тике
    if d.max_players and db.count(models.Player, draw_id=d.id) >= d.max_players:
        db.update(models.Draw, {"end_at": utcnow()}, id=d.id, status="active")
    return t["joined"], False


# ======================= ПУБЛИКАЦИЯ / ЗАВЕРШЕНИЕ =======================
def publish_draw(draw_id):
    """scheduled -> active и отправка поста. Возвращает (ok, ссылка_или_ошибка)."""
    if not db.update(models.Draw, {"status": "active"}, id=draw_id, status="scheduled"):
        return False, None  # уже опубликован другим потоком
    d = db.get_one(models.Draw, id=draw_id)
    text = d.text or ""
    link = join_link(d.id) if config.JOIN_LINK_IN_POST else None
    line = T(d.owner_id)["post"].get("join_link_line", "👉 {link}").format(link=link) if link else ""
    if line:
        limit = 4096 if d.file_type == "text" else 1024
        if plain_len(text) + plain_len(line) + 2 <= limit:
            text = f"{text}\n\n{line}" if text else line
            line = ""
    try:
        msg = send_post(d.channel_id, d.file_type, d.file_id, text, keyboard.join_kb(d, 0))
    except Exception as e:
        db.update(models.Draw, {"status": "draft"}, id=d.id)
        try:
            bot.send_message(d.owner_id, T(d.owner_id)["post"]["failed_post"].format(
                id=d.id, channel=esc(d.channel_name or ""), err=esc(short(str(e), 300))))
        except Exception:
            pass
        return False, str(e)
    db.update(models.Draw, {"message_id": msg.message_id, "shown_count": 0}, id=d.id)
    if line:  # ссылка не влезла в подпись — отправляем отдельным сообщением
        try:
            bot.send_message(d.channel_id, line, reply_to_message_id=msg.message_id,
                             disable_web_page_preview=True)
        except Exception:
            pass
    return True, post_link(db.get_one(models.Draw, id=d.id))


def pick_winners(draw_id, count):
    players = db.select_all(models.Player, order_by=models.Player.id, draw_id=draw_id)
    chosen = _rng.sample(players, min(count, len(players))) if players else []
    return len(players), [{"id": p.user_id, "name": p.name or p.username or p.user_id} for p in chosen]


def winners_text(winners):
    return "\n".join(
        f'{i}. <a href="tg://user?id={w["id"]}">{esc(w["name"])}</a>' for i, w in enumerate(winners, 1))


def _chunks(winners, size=40):
    return [winners[i:i + size] for i in range(0, len(winners), size)]


def announce(d, winners, reroll):
    """Публикует список победителей ответом на пост. True, если получилось."""
    t = T(d.owner_id)["post"]
    if winners:
        parts = [winners_text(c) for c in _chunks(winners)]
        parts[0] = t["reroll_title" if reroll else "winners_title"] + "\n" + parts[0]
    else:
        parts = [t["no_winners"]]
    for part in parts:
        try:
            bot.send_message(d.channel_id, part, reply_to_message_id=d.message_id)
        except Exception:
            try:
                bot.send_message(d.channel_id, part)
            except Exception:
                return False
    return True


def close_post_button(d, n_players):
    t = T(d.owner_id)["post"]
    try:
        bot.edit_message_reply_markup(
            d.channel_id, d.message_id,
            reply_markup=ikb([[(t["finished_button"].format(n=n_players), "done")]]))
    except Exception:
        pass


def finish_draw(draw_id, reroll=False):
    """Завершает розыгрыш (или переигрывает уже завершённый). Возвращает True/False."""
    if reroll:
        if not db.get_one(models.Draw, id=draw_id, status="finished"):
            return False
    elif not db.update(models.Draw, {"status": "finished"}, id=draw_id, status="active"):
        return False

    d = db.get_one(models.Draw, id=draw_id)
    n_players, winners = pick_winners(d.id, d.winners_count)
    db.update(models.Draw, {"winners": json.dumps(winners, ensure_ascii=False)}, id=d.id)
    if not reroll:
        close_post_button(d, n_players)

    posted = announce(d, winners, reroll)
    link = post_link(d)
    t = T(d.owner_id)["post"]

    # отчёт владельцу
    report = t["owner_report"].format(
        id=d.id, channel=esc(d.channel_name or ""), n=n_players,
        winners=winners_text(winners) if winners else t["no_winners"])
    if not posted:
        report += "\n\n" + t["announce_failed"]
    try:
        bot.send_message(d.owner_id, report, disable_web_page_preview=True)
    except Exception:
        pass

    # личное сообщение победителям (получат только те, кто запускал бота)
    for w in winners:
        try:
            wt = T(w["id"])["post"]
            text = wt["winner_dm"].format(channel=esc(d.channel_name or ""))
            if link:
                text += f"\n{link}"
            bot.send_message(w["id"], text, disable_web_page_preview=True)
        except Exception:
            pass
        time.sleep(0.05)
    return True


def players_csv(draw_id):
    players = db.select_all(models.Player, order_by=models.Player.id, draw_id=draw_id)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["n", "user_id", "username", "name", "joined_utc"])
    for i, p in enumerate(players, 1):
        w.writerow([i, p.user_id, p.username or "", p.name or "",
                    p.joined_at.strftime("%Y-%m-%d %H:%M:%S") if p.joined_at else ""])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = f"participants_{draw_id}.csv"
    return data, len(players)


def delete_draw(draw_id):
    db.delete(models.Player, draw_id=draw_id)
    db.delete(models.DrawChannel, draw_id=draw_id)
    db.delete(models.Draw, id=draw_id)
    _last_edit.pop(draw_id, None)


# ======================= ВОРКЕР =======================
def refresh_counter(d):
    if time.time() - _last_edit.get(d.id, 0) < config.COUNTER_EDIT_INTERVAL or not d.message_id:
        return
    n = db.count(models.Player, draw_id=d.id)
    if n == d.shown_count:
        return
    try:
        bot.edit_message_reply_markup(d.channel_id, d.message_id, reply_markup=keyboard.join_kb(d, n))
    except ApiTelegramException as e:
        if e.error_code == 429:
            retry = ((getattr(e, "result_json", None) or {}).get("parameters") or {}).get("retry_after", 5)
            _last_edit[d.id] = time.time() + retry
            return
    except Exception:
        pass
    db.update(models.Draw, {"shown_count": n}, id=d.id)
    _last_edit[d.id] = time.time()


def tick():
    now = utcnow()
    for d in db.select_all(models.Draw, status="scheduled"):
        if d.start_at is None or d.start_at <= now:
            _safe(publish_draw, d.id)
    for d in db.select_all(models.Draw, status="active"):
        if d.end_at and d.end_at <= now:
            _safe(finish_draw, d.id)
        else:
            _safe(refresh_counter, d)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        traceback.print_exc()


def worker_loop():
    while True:
        _safe(tick)
        time.sleep(config.WORKER_TICK)


def start_worker():
    threading.Thread(target=worker_loop, daemon=True, name="draw-worker").start()
