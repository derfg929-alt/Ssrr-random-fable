import csv
import io
import json
import logging
import queue
import random
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote

import telebot
from telebot.apihelper import ApiTelegramException

import captcha
import config
import keyboard
import models
from app import bot, db
from base import IntegrityError
from models import utcnow
from tool import (T, TD, boost_link, bot_username, esc, fmt_dt, ikb, short, tz_name,
                  user_tz)

log = logging.getLogger("bot.middleware")

OK_STATUSES = ("member", "administrator", "creator")
FINISH_MODES = ("time", "count", "both")
_bot_id = None
_last_edit = {}          # draw_id -> время последнего обновления счётчика
_synced = set()          # draw_id, у которых кнопка уже переведена на URL-вид
_ref_cache = None        # user_id -> число приглашённых (см. ref_counts)
_rng = random.SystemRandom()


def _mode(d):
    mode = getattr(d, "finish_mode", "time") or "time"
    return mode if mode in FINISH_MODES else "both"


def finish_mode(d):
    """both без цели фактически работает как time (совместимость со старыми розыгрышами)."""
    mode = _mode(d)
    if mode == "both" and not getattr(d, "max_players", 0):
        return "time"
    return mode


def invite_link(user_id):
    username = bot_username()
    return f"https://t.me/{username}?start=ref_{user_id}" if username else ""


def _fetch_boost_count(d, user_id):
    """Сколько бустов пользователь дал каналу. 0, если проверить не удалось."""
    if not getattr(d, "boost_enabled", 0):
        return 0
    try:
        result = bot.get_user_chat_boosts(d.channel_id, int(user_id))
        return len(getattr(result, "boosts", None) or [])
    except Exception:
        return 0


def refresh_boost_count(d, user_id):
    return _fetch_boost_count(d, user_id)


def ref_counts():
    """user_id пригласившего -> сколько пользователей он пригласил.

    Кэш: раньше здесь был полный скан всех пользователей на каждый вход в
    розыгрыш (сотни миллисекунд на большой базе).
    """
    global _ref_cache
    if _ref_cache is None:
        counts = {}
        for user in db.select_all(models.User):
            inviter = str(getattr(user, "referred_by", "") or "")
            if inviter:
                counts[inviter] = counts.get(inviter, 0) + 1
        _ref_cache = counts
    return _ref_cache


def invalidate_ref_counts():
    global _ref_cache
    _ref_cache = None


# ---- Фоновые задачи ----
_tasks = queue.Queue()
_bg_thread = None
_worker_thread = None
_stop = threading.Event()


def run_in_background(fn, *args):
    """Тяжёлая работа не должна занимать поток обработчика.

    До start_worker (например, в тестах) задача выполняется синхронно.
    """
    if _bg_thread is None or not _bg_thread.is_alive():
        return fn(*args)
    _tasks.put((fn, args))
    return None


def _bg_loop():
    while True:
        item = _tasks.get()
        try:
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception:
                log.exception("фоновая задача %s", getattr(fn, "__name__", fn))
        finally:
            _tasks.task_done()


def tickets_for(d, player, refs=0, boost=None):
    boost = getattr(player, "boost_count", 0) if boost is None else boost
    boost = boost or 0
    refs = refs or 0
    tickets = 1
    if getattr(d, "refs_enabled", 0):
        tickets += refs * config.REF_BONUS_TICKETS
    if getattr(d, "boost_enabled", 0):
        per_boost = getattr(d, "boost_tickets", 0) or config.BOOST_TICKETS_DEFAULT
        tickets += boost * per_boost
    return max(1, tickets)


def total_tickets(d, players=None, refs=None):
    players = players if players is not None else db.select_all(models.Player, draw_id=d.id)
    refs = refs if refs is not None else ref_counts()
    return sum(tickets_for(d, p, refs.get(str(p.user_id), 0), p.boost_count) for p in players)


def ticket_text(d, user_id, refs=None):
    player = db.get_one(models.Player, draw_id=d.id, user_id=str(user_id))
    if not player:
        return ""
    refs = refs if refs is not None else ref_counts()
    player_refs = refs.get(str(user_id), 0)
    tickets = tickets_for(d, player, player_refs, player.boost_count)
    total = total_tickets(d, refs=refs)
    chance = (tickets * 100.0 / total) if total else 0.0
    t = T(user_id)["post"]
    parts = [t["tickets_title"].format(tickets=tickets)]
    if getattr(d, "refs_enabled", 0) or getattr(d, "boost_enabled", 0):
        extras = [t["tickets_base"]]
        if getattr(d, "refs_enabled", 0):
            extras.append(t["tickets_refs"].format(refs=player_refs))
        if getattr(d, "boost_enabled", 0):
            extras.append(t["tickets_boost"].format(
                boost=player.boost_count or 0,
                per_boost=getattr(d, "boost_tickets", 0) or config.BOOST_TICKETS_DEFAULT,
            ))
        parts.append(" · ".join(extras))
    parts.append(t["tickets_total"].format(total=total, chance=f"{chance:.2f}"))
    return "\n".join(parts)


def send_ticket_info(user, d):
    """Сообщение участнику с его билетами и кнопками «пригласить»/«забустить»."""
    if not (getattr(d, "refs_enabled", 0) or getattr(d, "boost_enabled", 0)):
        return
    text = ticket_text(d, user.id)
    if not text:
        return
    t = T(user.id, getattr(user, "language_code", None))["post"]
    rows = []
    link = invite_link(user.id)
    if getattr(d, "refs_enabled", 0) and link:
        rows.append([(t["invite_button"], link)])
    if getattr(d, "boost_enabled", 0):
        boost = boost_link(d)
        if boost:
            rows.append([(t["boost_button"].format(n=getattr(d, "boost_tickets", 0) or config.BOOST_TICKETS_DEFAULT),
                          boost, "primary")])
        rows.append([(t["boost_check"], f"boost:{d.id}")])
    post = post_link(d)
    if post:
        rows.append([(t["share_button"], "https://t.me/share/url?url=" + quote(post, safe="")
                      + "&text=" + quote(t["share_text"], safe=""))])
    try:
        bot.send_message(user.id, text, reply_markup=ikb(rows) if rows else None,
                         disable_web_page_preview=True)
    except Exception:
        pass


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
        if user_id and not existing.added_by:
            fields["added_by"] = str(user_id)
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
    """Каналы, которые добавил этот пользователь и где бот администратор.

    Возвращает [(BotChat, пользователь_админ)].
    """
    key = str(user_id)
    cached = _chat_cache.get(key)
    if cached and time.time() - cached[0] < config.CHANNEL_CACHE_TTL:
        return cached[1]
    result = []
    for c in db.select_all(models.BotChat, order_by=models.BotChat.title):
        if str(c.added_by or "") != key:
            continue
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
        if len(result) >= config.MAX_LISTED_CHANNELS:
            break
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
    mode = finish_mode(d)
    display_mode = mode
    if display_mode == "count":
        end_text = s["by_count"].format(n=d.max_players)
    elif display_mode == "both":
        end_text = f"{fmt_dt(d.end_at, tz)} / {s['by_count'].format(n=d.max_players)}"
    else:
        end_text = fmt_dt(d.end_at, tz)
    limit = str(d.max_players) if d.max_players else t["common"]["unlimited"]
    refs_text = s["on"] if d.refs_enabled else s["off"]
    boost_text = s["boost_on"].format(n=d.boost_tickets or config.BOOST_TICKETS_DEFAULT) \
        if d.boost_enabled else s["off"]
    lines = [
        s["title"].format(id=d.id, status=f"{t['status_icons'][d.status]} {t['status'][d.status]}"),
        "",
        f"<b>{s['channel']}:</b> {esc(d.channel_name or '')}",
        f"<b>{s['mode']}:</b> {s['mode_' + display_mode]}",
        f"<b>{s['start']}:</b> {fmt_dt(d.start_at, tz) if d.start_at else s['on_publish']}",
        f"<b>{s['end']}:</b> {end_text}",
        f"<b>{s['tz']}:</b> {tz_name(tz, uid)}",
        f"<b>{s['winners']}:</b> {d.winners_count}",
        f"<b>{s['target'] if display_mode in ('count', 'both') else s['limit']}:</b> {limit}",
        f"<b>{s['refs']}:</b> {refs_text}",
        f"<b>{s['boost']}:</b> {boost_text}",
        f"<b>{s['button']}:</b> {esc(d.button_text or '')}",
        f"<b>{s['subs']}:</b> " + (", ".join(esc(c.title or "") for c in subs) or t["common"]["none"]),
    ]
    if d.status in ("active", "finished"):
        players = db.select_all(models.Player, draw_id=d.id)
        lines.append(f"<b>{s['players']}:</b> {len(players)}")
        lines.append(f"<b>{s['tickets']}:</b> {total_tickets(d, players=players)}")
        boosts = sum(p.boost_count or 0 for p in players)
        if d.boost_enabled and boosts:
            lines.append(f"<b>{s['boosts']}:</b> {boosts}")
    if d.status == "finished" and (d.rerolls or 0):
        lines.append(f"<b>{s['rerolls']}:</b> {d.rerolls}")
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


def _join_precheck(user, draw_id):
    """Проверяет, может ли пользователь участвовать. (Draw, None) или (None, (текст, alert))."""
    t = T(user.id, user.language_code)["post"]
    try:
        draw_id = int(draw_id)
    except (TypeError, ValueError):
        return None, (t["finished"], True)
    d = db.get_one(models.Draw, id=draw_id)
    if not d or d.status != "active":
        return None, (t["finished"], True)
    if db.get_one(models.Player, draw_id=d.id, user_id=str(user.id)):
        return None, (t["already"], False)
    if d.max_players and db.count(models.Player, draw_id=d.id) >= d.max_players:
        return None, (t["full"], True)

    missing, err = missing_subscriptions(d, user.id)
    if err:
        return None, (t["check_failed"], True)
    if missing:
        return None, (short(t["not_subscribed"].format(channels=", ".join(missing)), 200), True)
    return d, None


def join_draw_by_id(user, draw_id):
    """Записывает участника (после капчи или при выключенной капче)."""
    t = T(user.id, user.language_code)["post"]
    d, err = _join_precheck(user, draw_id)
    if err:
        return err

    name = " ".join(x for x in (user.first_name, user.last_name) if x) or user.username or str(user.id)
    boost_count = _fetch_boost_count(d, user.id)

    def has_room():
        return not (d.max_players and db.count(models.Player, draw_id=d.id) >= d.max_players)

    try:
        player = db.new(models.Player, _guard=has_room, draw_id=d.id, user_id=str(user.id),
                        name=short(name, 64), username=user.username, boost_count=boost_count)
    except IntegrityError:
        return t["already"], False
    if player is None:
        return t["full"], True

    # count/both завершит воркер на ближайшем тике, когда увидит достигнутую цель
    send_ticket_info(user, d)
    return t["joined"], False


def start_join(call):
    """Нажатие «Участвовать» в канале: проверяем условия и присылаем капчу в личку."""
    user = call.from_user
    try:
        draw_id = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        return T(user.id, user.language_code)["post"]["finished"], True
    return begin_join(user, draw_id)


def begin_join(user, draw_id):
    """Проверка условий + отправка капчи. Возвращает (текст, alert) для ответа на callback."""
    d, err = _join_precheck(user, draw_id)
    if err:
        return err
    if not config.CAPTCHA_ENABLED:
        return join_draw_by_id(user, d.id)
    # Повторное нажатие не должно плодить новые капчи: показываем уже выданную.
    existing = captcha.get(user.id)
    if existing and existing.get("draw_id") == d.id:
        return _send_captcha(user, d, existing)
    if not captcha.allow(user.id):
        return T(user.id, user.language_code)["captcha"]["too_many_attempts"], True
    return _send_captcha(user, d)


def take_pending(user_id):
    """Попытка участия, которую бот не смог начать сам (пользователь не открывал бота)."""
    return captcha.pop_pending(user_id)


def _captcha_caption(uid, item):
    t = T(uid)["captcha"]
    color_key, shape_key = item["target"]
    target = t["target"].format(color=t["colors"][color_key], shape=t["shapes"][shape_key])
    return t["caption"].format(target=target)


def _send_captcha(user, d, item=None):
    t = T(user.id, user.language_code)["captcha"]
    item = item or captcha.make(d.id)
    kb = keyboard.captcha_kb(d.id, item["nonce"])
    try:
        msg = bot.send_photo(user.id, io.BytesIO(item["png"]), caption=_captcha_caption(user.id, item),
                             parse_mode="HTML", reply_markup=kb)
    except Exception:
        # Бот не может писать первым: запомним попытку и попросим открыть бота.
        captcha.set_pending(user.id, d.id)
        username = bot_username()
        return t["open_bot"].format(bot=f"@{username}" if username else "бот"), True
    captcha.save(user.id, item, msg.chat.id, msg.message_id)
    return "", False


def handle_captcha(call):
    """Нажатие номера на капче. Возвращает (текст, alert) для ответа на callback."""
    user = call.from_user
    uid = user.id
    t = T(uid, user.language_code)["captcha"]
    p = (call.data or "").split(":")
    if len(p) < 4:
        return "", False
    try:
        draw_id, idx = int(p[1]), int(p[3])
    except ValueError:
        return "", False

    item = captcha.get(uid)
    if not item or item.get("nonce") != p[2] or item.get("draw_id") != draw_id:
        return t["expired"], True

    if idx != item["answer"]:
        tries = item.get("tries", 0) + 1
        if tries >= captcha.MAX_TRIES:
            captcha.drop(uid)
            _delete_message(uid, item.get("message_id"))
            return t["too_many"], True
        fresh = captcha.make(draw_id)
        fresh["tries"] = tries
        captcha.save(uid, fresh, item.get("chat_id", uid), item.get("message_id"))
        _replace_captcha(uid, item.get("message_id"), fresh)
        return t["wrong"], False

    captcha.drop(uid)
    _delete_message(uid, item.get("message_id"))
    text, alert = join_draw_by_id(user, draw_id)
    try:
        bot.send_message(uid, text, reply_markup=keyboard.main_menu(uid), disable_web_page_preview=True)
    except Exception:
        pass
    return "", False


def _delete_message(chat_id, message_id):
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def _replace_captcha(uid, message_id, item):
    caption = _captcha_caption(uid, item)
    kb = keyboard.captcha_kb(item["draw_id"], item["nonce"])
    if message_id:
        try:
            bot.edit_message_media(
                telebot.types.InputMediaPhoto(io.BytesIO(item["png"]), caption=caption, parse_mode="HTML"),
                chat_id=uid, message_id=message_id, reply_markup=kb)
            return
        except Exception:
            _delete_message(uid, message_id)
    try:
        msg = bot.send_photo(uid, io.BytesIO(item["png"]), caption=caption, parse_mode="HTML", reply_markup=kb)
    except Exception:
        return
    captcha.save(uid, item, msg.chat.id, msg.message_id)


# ======================= ПУБЛИКАЦИЯ / ЗАВЕРШЕНИЕ =======================
def publish_draw(draw_id):
    """scheduled -> active и отправка поста. Возвращает (ok, ссылка_или_ошибка)."""
    if not db.update(models.Draw, {"status": "active"}, id=draw_id, status="scheduled"):
        return False, None  # уже опубликован другим потоком
    d = db.get_one(models.Draw, id=draw_id)
    text = d.text or ""
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
    _synced.add(d.id)
    return True, post_link(db.get_one(models.Draw, id=d.id))


def load_winners(d):
    try:
        data = json.loads(getattr(d, "winners", None) or "[]")
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def load_reroll_log(d):
    try:
        data = json.loads(getattr(d, "reroll_log", None) or "[]")
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def results_text(d, uid):
    """Итоги: текущие победители + все, кого заменили переигровками."""
    t = T(uid)["post"]
    tz = user_tz(uid)
    winners = load_winners(d)
    history = load_reroll_log(d)
    lines = [t["results_title"].format(id=d.id), "", t["current_winners"]]
    if winners:
        lines.extend(winner_line(i, w) for i, w in enumerate(winners, 1))
    else:
        lines.append(t["no_winners"])

    lines.append("")
    if history:
        lines.append(t["rerolled_winners"])
        total_rounds = len(history)
        for shown, entry in enumerate(reversed(history), 1):
            if not isinstance(entry, dict):
                continue
            round_no = total_rounds - shown + 1
            at = entry.get("at") or ""
            try:
                at = fmt_dt(datetime.fromisoformat(at), tz)
            except (TypeError, ValueError):
                pass
            lines.append(t["reroll_round"].format(n=round_no, date=at))
            old = entry.get("winners") or []
            if old:
                lines.extend(winner_line(i, w) for i, w in enumerate(old, 1))
            else:
                lines.append(t["no_winners"])
    elif (d.rerolls or 0):
        lines.append(t["reroll_history_missing"].format(n=d.rerolls))
    else:
        lines.append(t["no_rerolls"])

    lines.append("")
    lines.append(t["players_count"].format(n=db.count(models.Player, draw_id=d.id)))
    if history and (d.rerolls or 0) > len(history):
        lines.append(t["reroll_history_missing"].format(n=d.rerolls))
    return "\n".join(lines)


def winner_line(index, w):
    """Строка победителя: @username (ссылка), иначе имя со ссылкой tg://user."""
    if not isinstance(w, dict):
        return f"{index}. {esc(str(w))}"
    username = str(w.get("username") or "").lstrip("@")
    name = esc(str(w.get("name") or ""))
    if username:
        label = f"@{esc(username)}"
        return f'{index}. <a href="https://t.me/{esc(username)}">{label}</a>' + (f" — {name}" if name else "")
    uid = w.get("id")
    if uid:
        return f'{index}. <a href="tg://user?id={esc(str(uid))}">{name or esc(str(uid))}</a>'
    return f"{index}. {name}"


def winners_text(winners):
    return "\n".join(winner_line(i, w) for i, w in enumerate(winners, 1))


def _winner_chunks(winners, limit=3500):
    chunks, cur, size = [], [], 0
    for i, w in enumerate(winners, 1):
        line = winner_line(i, w)
        if cur and size + len(line) + 1 > limit:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def split_text(text, limit=3500):
    """Режет длинный HTML-текст по строкам, чтобы Telegram не ругался на лимит 4096."""
    parts, cur, size = [], [], 0
    for line in (text or "").split("\n"):
        while len(line) > limit:
            if cur:
                parts.append("\n".join(cur))
                cur, size = [], 0
            parts.append(line[:limit])
            line = line[limit:]
        if cur and size + len(line) + 1 > limit:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts or [""]


def _weighted_sample(pool, weights, count):
    """Выбор без повторов с учётом весов (билетов)."""
    pool, weights, chosen = list(pool), list(weights), []
    while pool and len(chosen) < count:
        total = sum(weights)
        if total <= 0:
            idx = _rng.randrange(len(pool))
        else:
            point = _rng.uniform(0, total)
            acc, idx = 0.0, len(pool) - 1
            for i, weight in enumerate(weights):
                acc += weight
                if point < acc:
                    idx = i
                    break
        chosen.append(pool.pop(idx))
        weights.pop(idx)
    return chosen


def pick_winners(draw_id, count, exclude_ids=None):
    """Выбирает победителей. Возвращает (всего_участников, победители, был_ли_откат)."""
    d = db.get_one(models.Draw, id=draw_id)
    players = db.select_all(models.Player, order_by=models.Player.id, draw_id=draw_id)
    pool, fallback = players, False
    if exclude_ids:
        exclude = {str(x) for x in exclude_ids}
        fresh = [p for p in players if str(p.user_id) not in exclude]
        if len(fresh) >= count:
            pool = fresh
        else:
            # Новых участников не хватает — выбираем из всех, чтобы выдать нужное число.
            fallback = bool(players)

    refs = ref_counts() if (d and getattr(d, "refs_enabled", 0)) else {}
    weights = [tickets_for(d, p, refs.get(str(p.user_id), 0), p.boost_count) if d else 1
               for p in pool]
    chosen = _weighted_sample(pool, weights, min(count, len(pool))) if pool else []
    winners = [{"id": p.user_id, "name": p.name or p.username or p.user_id,
                "username": p.username or ""} for p in chosen]
    return len(players), winners, fallback


def announce(d, winners, reroll, n_players=0):
    """Публикует список победителей ответом на пост. (успех, id последнего сообщения)."""
    t = TD(d)["post"]
    if winners:
        parts = _winner_chunks(winners)
        parts[0] = t["reroll_title" if reroll else "winners_title"] + "\n" + parts[0]
    else:
        parts = [t["no_winners"]]
    if n_players:
        parts[-1] += "\n\n" + t["players_count"].format(n=n_players)
    username = bot_username()
    if username:
        parts[-1] += "\n\n" + t["more_giveaways"].format(bot=f"@{username}")
    markup = keyboard.results_kb(d.owner_id, d.id, back=False, bot=True, button=True)
    last_id = None
    for idx, part in enumerate(parts):
        kw = {}
        if d.message_id:
            kw["reply_to_message_id"] = d.message_id
        if idx == len(parts) - 1 and markup is not None:
            kw["reply_markup"] = markup
        try:
            msg = bot.send_message(d.channel_id, part, **kw)
        except Exception:
            kw.pop("reply_to_message_id", None)
            try:
                msg = bot.send_message(d.channel_id, part, **kw)
            except Exception:
                return False, None
        last_id = getattr(msg, "message_id", last_id)
    return True, last_id


def close_post_button(d, n_players):
    t = TD(d)["post"]
    rows = [[(t["finished_button"].format(n=n_players), "done")],
            [(t["results_button"], f"res:{d.id}")]]
    username = bot_username()
    if username:
        rows.append([(t["bot_button"], f"https://t.me/{username}")])
    try:
        bot.edit_message_reply_markup(d.channel_id, d.message_id, reply_markup=ikb(rows))
    except Exception:
        pass


def finish_draw(draw_id, reroll=False, exclude_previous=False):
    """Завершает розыгрыш (или переигрывает уже завершённый). Возвращает True/False."""
    if reroll:
        d = db.get_one(models.Draw, id=draw_id, status="finished")
        if not d:
            return False
    else:
        if not db.update(models.Draw, {"status": "finished", "finished_at": utcnow()},
                         id=draw_id, status="active"):
            return False
        d = db.get_one(models.Draw, id=draw_id)

    previous = load_winners(d)
    exclude_ids = ({str(w.get("id")) for w in previous if isinstance(w, dict)}
                   if exclude_previous else None)
    n_players, winners, fallback = pick_winners(d.id, d.winners_count, exclude_ids)
    rerolls = (d.rerolls or 0) + (1 if reroll else 0)
    values = {"winners": json.dumps(winners, ensure_ascii=False), "rerolls": rerolls}
    if reroll and previous:
        history = load_reroll_log(d)
        history.append({"at": utcnow().isoformat(), "winners": previous})
        values["reroll_log"] = json.dumps(history[-config.MAX_REROLL_LOG:], ensure_ascii=False)
    if not d.finished_at:
        values["finished_at"] = utcnow()
    db.update(models.Draw, values, id=d.id)
    d = db.get_one(models.Draw, id=d.id)
    if not reroll:
        close_post_button(d, n_players)

    posted, last_id = announce(d, winners, reroll, n_players)
    if posted and config.PIN_RESULTS and last_id:
        try:
            bot.pin_chat_message(d.channel_id, last_id, disable_notification=True)
        except Exception:
            log.debug("не удалось закрепить итоги #%s", d.id, exc_info=True)
    link = post_link(d)
    t = T(d.owner_id)["post"]

    # отчёт владельцу (режем на части: победителей может быть много)
    report = t["owner_report_reroll" if reroll else "owner_report"].format(
        id=d.id, channel=esc(d.channel_name or ""), n=n_players, rerolls=rerolls,
        winners=winners_text(winners) if winners else t["no_winners"])
    if fallback:
        report += "\n\n" + t["reroll_fallback"]
    if not posted:
        report += "\n\n" + t["announce_failed"]
    for part in split_text(report):
        try:
            bot.send_message(d.owner_id, part, disable_web_page_preview=True)
        except Exception:
            break

    # личные сообщения победителям — в фоне, чтобы не держать поток обработчика
    run_in_background(_notify_winners, d.id, winners, link)
    _synced.discard(d.id)
    _last_edit.pop(d.id, None)
    return True


def _notify_winners(draw_id, winners, link):
    """Личное сообщение победителям (получат только те, кто запускал бота)."""
    d = db.get_one(models.Draw, id=draw_id)
    if not d:
        return
    username = bot_username()
    for w in winners:
        try:
            wt = T(w["id"])["post"]
            text = wt["winner_dm"].format(channel=esc(d.channel_name or ""))
            if link:
                text += f"\n{link}"
            if username:
                text += "\n\n" + wt["more_giveaways"].format(bot=f"@{username}")
            bot.send_message(w["id"], text, disable_web_page_preview=True,
                             reply_markup=keyboard.bot_kb(w["id"]))
        except Exception:
            log.debug("не удалось написать победителю %s", w.get("id"), exc_info=True)
        time.sleep(0.05)


def cancel_draw(draw_id):
    """Отменяет запланированный или активный розыгрыш."""
    d = db.get_one(models.Draw, id=draw_id)
    if not d or d.status not in ("scheduled", "active"):
        return False
    old = d.status
    if not db.update(models.Draw, {"status": "cancelled"}, id=draw_id, status=old):
        return False
    t = TD(d)["post"]
    if old == "active" and d.message_id:
        n = db.count(models.Player, draw_id=d.id)
        rows = [[(t["cancelled_button"].format(n=n), "done")]]
        username = bot_username()
        if username:
            rows.append([(t["bot_button"], f"https://t.me/{username}")])
        try:
            bot.edit_message_reply_markup(d.channel_id, d.message_id, reply_markup=ikb(rows))
        except Exception:
            pass
        try:
            bot.send_message(d.channel_id, t["cancelled_announce"],
                             reply_to_message_id=d.message_id)
        except Exception:
            pass
    _synced.discard(d.id)
    _last_edit.pop(d.id, None)
    return True


def register_referral(invited_id, inviter_id):
    """Запоминает, кто пригласил пользователя, и уведомляет пригласившего."""
    try:
        inviter_id = str(int(inviter_id))
    except (TypeError, ValueError):
        return
    invited_id = str(invited_id)
    if inviter_id == invited_id or not db.get_one(models.User, user_id=inviter_id):
        return
    invited = db.get_one(models.User, user_id=invited_id)
    if not invited or invited.referred_by:
        return
    db.update(models.User, {"referred_by": inviter_id}, user_id=invited_id)
    if _ref_cache is not None:
        _ref_cache[inviter_id] = _ref_cache.get(inviter_id, 0) + 1
    total = referral_count(inviter_id)
    try:
        bot.send_message(inviter_id, T(inviter_id)["menu"]["invited_notice"].format(invited=total),
                         disable_web_page_preview=True)
    except Exception:
        pass


def referral_count(user_id):
    return db.count(models.User, referred_by=str(user_id))


def players_csv(draw_id):
    players = db.select_all(models.Player, order_by=models.Player.id, draw_id=draw_id)
    winner_ids = {str(w.get("id")) for w in load_winners(db.get_one(models.Draw, id=draw_id) or models.Draw())}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["n", "user_id", "username", "name", "is_winner", "joined_utc"])
    for i, p in enumerate(players, 1):
        w.writerow([i, p.user_id, p.username or "", p.name or "",
                    "yes" if str(p.user_id) in winner_ids else "",
                    p.joined_at.strftime("%Y-%m-%d %H:%M:%S") if p.joined_at else ""])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = f"participants_{draw_id}.csv"
    return data, len(players)


def delete_draw(draw_id):
    db.delete(models.Player, draw_id=draw_id)
    db.delete(models.DrawChannel, draw_id=draw_id)
    db.delete(models.Draw, id=draw_id)
    _last_edit.pop(draw_id, None)
    _synced.discard(draw_id)


# ======================= ВОРКЕР =======================
def refresh_counter(d):
    if not d.message_id:
        return
    n = db.count(models.Player, draw_id=d.id)
    first = d.id not in _synced
    if not first and time.time() - _last_edit.get(d.id, 0) < config.COUNTER_EDIT_INTERVAL:
        return
    if not first and n == d.shown_count:
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
    _synced.add(d.id)
    db.update(models.Draw, {"shown_count": n}, id=d.id)
    _last_edit[d.id] = time.time()


def tick():
    now = utcnow()
    captcha.sweep()
    for d in db.select_all(models.Draw, status="scheduled"):
        if d.start_at is None or d.start_at <= now:
            _safe(publish_draw, d.id)
    for d in db.select_all(models.Draw, status="active"):
        mode = finish_mode(d)
        players = db.count(models.Player, draw_id=d.id)
        reached = mode in ("count", "both") and d.max_players and players >= d.max_players
        timed = mode in ("time", "both") and d.end_at and d.end_at <= now
        if reached or timed:
            _safe(finish_draw, d.id)
        else:
            _safe(refresh_counter, d)
            _safe(remind_owner, d)


def remind_owner(d):
    """Напоминание владельцу за 24 часа и за час до конца розыгрыша."""
    if not config.REMINDERS_ENABLED or not d.end_at:
        return
    left = (d.end_at - utcnow()).total_seconds()
    if left <= 0:
        return
    if left <= 3600:
        flag, key = "reminded_1h", "reminder_1h"
    elif left <= 86400:
        flag, key = "reminded_24h", "reminder_24h"
    else:
        return
    if getattr(d, flag):
        return
    db.update(models.Draw, {flag: 1}, id=d.id)
    t = T(d.owner_id)["post"]
    tz = user_tz(d.owner_id)
    text = t[key].format(id=d.id, channel=esc(d.channel_name or ""),
                         players=db.count(models.Player, draw_id=d.id),
                         end=fmt_dt(d.end_at, tz), tz=tz_name(tz, d.owner_id))
    try:
        bot.send_message(d.owner_id, text, disable_web_page_preview=True)
    except Exception:
        log.debug("не удалось отправить напоминание по #%s", d.id, exc_info=True)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        log.exception("ошибка в %s", getattr(fn, "__name__", fn))


def worker_loop():
    while not _stop.is_set():
        _safe(tick)
        _stop.wait(config.WORKER_TICK)


def start_worker():
    global _bg_thread, _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _stop.clear()
        _worker_thread = threading.Thread(target=worker_loop, daemon=True, name="draw-worker")
        _worker_thread.start()
    if _bg_thread is None or not _bg_thread.is_alive():
        _bg_thread = threading.Thread(target=_bg_loop, daemon=True, name="draw-bg")
        _bg_thread.start()


def stop_worker():
    _stop.set()
    if _bg_thread is not None and _bg_thread.is_alive():
        _tasks.put(None)


def delete_user_data(user_id):
    """Удаляет данные пользователя по его запросу (/mydata)."""
    uid = str(user_id)
    removed = db.delete(models.Player, user_id=uid)
    db.delete(models.UserState, user_id=uid)
    for user in db.select_all(models.User, referred_by=uid):
        db.update(models.User, {"referred_by": ""}, user_id=user.user_id)
    db.delete(models.User, user_id=uid)
    captcha.forget(uid)
    invalidate_ref_counts()
    return removed
