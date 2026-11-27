import json
from datetime import timedelta

import config
import keyboard
import main
import middleware
import models
from models import utcnow
from tool import TEXTS, is_users_admin


def _flatten(data, prefix=""):
    out = {}
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, name))
        else:
            out[name] = value
    return out


def _draw(db, **kwargs):
    defaults = dict(owner_id="1", channel_id="-1001", channel_name="Ch",
                    channel_username="ch", text="Prize", status="active",
                    winners_count=1, max_players=0, language="RU",
                    end_at=utcnow() + timedelta(hours=1))
    defaults.update(kwargs)
    return db.new(models.Draw, **defaults)


def test_locales_have_same_keys():
    ru = _flatten(json.load(open("RU.json", encoding="utf-8")))
    eng = _flatten(json.load(open("ENG.json", encoding="utf-8")))
    assert set(ru) == set(eng)


def _reply_labels(markup):
    labels = set()
    for row in markup.keyboard:
        for button in row:
            labels.add(button["text"] if isinstance(button, dict) else button.text)
    return labels


def test_menu_has_help_and_no_tickets():
    labels = _reply_labels(keyboard.main_menu(1))
    assert TEXTS["RU"]["menu"]["buttons"]["help"] in labels
    assert not any("билет" in label.lower() for label in labels)


def test_menu_actions_cover_new_buttons():
    assert main.MENU_HANDLERS["admin"] is main.m_admin
    assert main.MENU_HANDLERS["help"] is main.m_help
    assert "tickets" not in main.MENU_HANDLERS


def test_admin_button_only_for_admin(monkeypatch):
    label = TEXTS["RU"]["menu"]["buttons"]["admin"]
    assert label not in _reply_labels(keyboard.main_menu(1))
    monkeypatch.setattr(config, "USERS_ADMIN_IDS", {"1"})
    assert label in _reply_labels(keyboard.main_menu(1))


def test_owner_id_is_admin():
    assert is_users_admin(8039111975)
    assert "8039111975" in config.ADMIN_IDS
    assert "8039111975" in config.USERS_ADMIN_IDS


def test_admin_panel_keyboard_has_all_sections():
    labels = _reply_labels(keyboard.admin_kb(1))
    t = TEXTS["RU"]["admin"]
    assert {t["live"], t["stats"], t["users"], t["chats"], t["draws"],
            t["export"], t["backup"], t["restore"], t["refresh"]} <= labels


def test_show_admin_panel(bot):
    main.show_admin(1)
    assert bot.sent[-1][1] == TEXTS["RU"]["admin"]["title"]


def test_show_stats_counts(db, bot):
    db.new(models.User, user_id="1", user_name="A")
    db.new(models.User, user_id="2", user_name="B")
    db.new(models.BotChat, chat_id="-1001", title="Ch", type="channel", can_post=1)
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="A")

    main.show_stats(1)

    assert bot.sent[-1][1] == TEXTS["RU"]["admin"]["stats_title"].format(
        users=2, chats=1, draws=1, active=1, players=1, tickets=1)


def test_show_stats_chats_lists_bot_chats_with_owner(db, bot):
    db.new(models.User, user_id="1", user_name="Ann", username="ann")
    db.new(models.BotChat, chat_id="-1001", title="My Channel", type="channel",
           can_post=1, added_by="1")
    db.new(models.BotChat, chat_id="-2002", title="My Group", type="supergroup", can_post=0)

    main.show_stats_chats(1)

    text = bot.sent[-1][1]
    assert "My Channel" in text and "My Group" in text
    assert "✅" in text and "🔒" in text
    assert "@ann" in text


def test_show_stats_draws_counts_by_status(db, bot):
    _draw(db, status="active")
    _draw(db, status="finished")
    _draw(db, status="draft")

    main.show_stats_draws(1)

    text = bot.sent[-1][1]
    assert TEXTS["RU"]["admin"]["draws_title"].format(
        draws=3, draft="черновик", n_draft=1, scheduled="запланирован", n_scheduled=0,
        active="активен", n_active=1, finished="завершён", n_finished=1,
        cancelled="отменён", n_cancelled=0, players=0, uniq=0, tickets=0) == text


def test_show_live_panel(db, bot):
    db.new(models.User, user_id="1", user_name="Ann")
    db.new(models.BotChat, chat_id="-1001", title="Ch", type="channel", can_post=1)
    draw = _draw(db, channel_name="Prize Channel")
    db.new(models.Player, draw_id=draw.id, user_id="1", name="Ann")

    main.show_live(1)

    text = bot.sent[-1][1]
    assert "👥 Пользователей: <b>1</b>" in text
    assert "🆕 Участвовали за 24ч: <b>1</b>" in text
    assert "🎉 Активных розыгрышей: <b>1</b>" in text
    assert f"#{draw.id} Prize Channel" in text
    assert "👥 1" in text


def test_show_live_panel_without_draws(bot):
    main.show_live(1)
    assert TEXTS["RU"]["admin"]["live_no_draws"] in bot.sent[-1][1]


def test_users_csv_contains_users_and_draw_counts(db):
    db.new(models.User, user_id="1", user_name="Ann", username="ann")
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="Ann")

    text = main._users_csv().getvalue().decode("utf-8-sig")

    assert "user_id;username;name;language;referred_by;draws" in text
    assert "1;ann;Ann;RU;;1" in text


def test_admin_callbacks_ignored_for_non_admin(bot, make_call):
    for data in ("st:main", "ad:main", "ad:live", "ad:export", "ad:backup"):
        main.route_callback(make_call(data, uid=5))
    assert bot.sent == []
    assert bot.documents == []


def test_owner_opens_admin_panel(bot, make_call):
    main.route_callback(make_call("ad:main", uid=8039111975))
    assert bot.edits[-1][2] == TEXTS["RU"]["admin"]["title"]


def test_admin_restore_hint(bot, make_call):
    main.route_callback(make_call("ad:restore", uid=8039111975))
    assert bot.sent[-1][1] == TEXTS["RU"]["admin"]["restore_help"]


def test_join_kb_uses_draw_language(db):
    draw = db.new(models.Draw, owner_id="1", channel_id="-1", channel_name="C",
                  text="t", language="ENG", status="active")
    labels = [button.text for row in keyboard.join_kb(draw, preview=True).keyboard
              for button in row]
    assert any("Join" in label for label in labels)


def test_draw_summary_shows_tickets(db):
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="Ann")
    text = middleware.draw_summary(1, draw)
    assert TEXTS["RU"]["draw"]["summary"]["tickets"] in text


def test_mydata_text(db):
    db.new(models.User, user_id="5", user_name="Ann", username="ann")
    text = main.mydata_text(5)
    assert "5" in text and "@ann" in text


def test_delete_my_data_callback(db, bot, make_call):
    db.new(models.User, user_id="5", user_name="Ann")
    db.new(models.User, user_id="6", user_name="Bob", referred_by="5")
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="5", name="Ann")

    main.handle_my_data(make_call("mydel:yes", uid=5), ["mydel", "yes"])

    assert db.get_one(models.User, user_id="5") is None
    assert db.get_one(models.User, user_id="6").referred_by == ""
    assert db.count(models.Player, user_id="5") == 0
    assert bot.answers[-1][1] is True


def test_delete_my_data_asks_for_confirmation(db, bot, make_call):
    main.handle_my_data(make_call("mydel:ask", uid=5), ["mydel", "ask"])
    assert bot.sent[-1][1] == TEXTS["RU"]["menu"]["delete_confirm"]


def test_unsupported_message_gets_hint(bot, make_message):
    main.h_unsupported(make_message(uid=5, content_type="voice", text=None))
    assert bot.sent[-1][1] == TEXTS["RU"]["common"]["unsupported"]


def test_create_draw_sets_language(db, bot):
    db.new(models.User, user_id="5", user_name="Ann", language="ENG")
    data = {"channel_id": "-1001", "channel_name": "Ch", "text": "hi",
            "winners_count": 1, "finish_mode": "time",
            "end_at": (utcnow() + timedelta(hours=2)).isoformat()}

    main._create_draw(5, data)

    draw = db.select_all(models.Draw)[0]
    assert draw.status == "draft"
    assert draw.language == "ENG"


def test_publish_rejects_more_winners_than_target(db, bot, make_call):
    draw = _draw(db, status="draft", finish_mode="count", max_players=3, winners_count=5)
    main.do_publish(5, draw, make_call(f"pub:{draw.id}", uid=5))

    text, alert = bot.answers[-1]
    assert alert is True and "Победителей" in text
    assert db.get_one(models.Draw, id=draw.id).status == "draft"


def test_publish_warns_about_private_channel(db, bot, make_call):
    draw = _draw(db, status="draft", channel_username="")
    main.do_publish(5, draw, make_call(f"pub:{draw.id}", uid=5))

    assert db.get_one(models.Draw, id=draw.id).status == "active"
    assert any(TEXTS["RU"]["draw"]["private_channel_note"] in text
               for chat, text, _ in bot.sent if str(chat) == "5")


def test_publish_requires_end_for_time_mode(db, bot, make_call):
    draw = _draw(db, status="draft", end_at=None)
    main.do_publish(5, draw, make_call(f"pub:{draw.id}", uid=5))
    assert bot.answers[-1] == (TEXTS["RU"]["draw"]["need_end"], True)
    assert db.get_one(models.Draw, id=draw.id).status == "draft"


def test_full_wizard_flow(db, bot, make_message, make_call, monkeypatch):
    """Мастер -> публикация -> участие -> завершение, без обращения к Telegram."""
    uid = 5
    bot.chat_members[("-1001", str(uid))] = "administrator"
    bot.chat_members[("-1001", "42")] = "administrator"

    main.begin_create(uid, "-1001")
    assert fsm_state(uid) == "c_text"

    main.router(make_message(text="Призовой фонд", uid=uid))
    main.router(make_message(text=TEXTS["RU"]["draw"]["skip_media"], uid=uid))
    main.router(make_message(text="1", uid=uid))
    main.router(make_message(text=TEXTS["RU"]["draw"]["start_now_btn"], uid=uid))
    main.route_callback(make_call("mode:0:time", uid=uid))
    assert fsm_state(uid) == "c_end"
    main.router(make_message(text="01.01.2030 10:00", uid=uid))

    draw = db.select_all(models.Draw)[0]
    assert draw.status == "draft"
    assert draw.text == "Призовой фонд"
    assert draw.winners_count == 1
    assert fsm_state(uid) == "idle"

    main.route_callback(make_call(f"pub:{draw.id}", uid=uid))
    draw = db.get_one(models.Draw, id=draw.id)
    assert draw.status == "active"
    assert draw.message_id is not None

    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    main.route_callback(make_call(f"join:{draw.id}", uid=700))
    assert db.count(models.Player, draw_id=draw.id) == 1

    main.do_action(uid, db.get_one(models.Draw, id=draw.id), "fin",
                   make_call(f"do:{draw.id}:fin", uid=uid))

    draw = db.get_one(models.Draw, id=draw.id)
    assert draw.status == "finished"
    winners = json.loads(draw.winners)
    assert [w["id"] for w in winners] == ["700"]


def fsm_state(uid):
    from app import fsm

    return fsm.get_state(uid)[0]
