import json
from datetime import timedelta

import config
import keyboard
import main
import middleware
import models
from models import utcnow
from tool import TEXTS


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


def test_menu_has_tickets_and_help():
    labels = _reply_labels(keyboard.main_menu(1))
    assert TEXTS["RU"]["menu"]["buttons"]["tickets"] in labels
    assert TEXTS["RU"]["menu"]["buttons"]["help"] in labels


def test_menu_actions_cover_new_buttons():
    assert main.MENU_HANDLERS["tickets"] is main.m_tickets
    assert main.MENU_HANDLERS["help"] is main.m_help


def test_join_kb_uses_draw_language(db):
    draw = db.new(models.Draw, owner_id="1", channel_id="-1", channel_name="C",
                  text="t", language="ENG", status="active")
    labels = [button.text for row in keyboard.join_kb(draw, preview=True).keyboard
              for button in row]
    assert any("Join" in label for label in labels)


def test_tickets_kb_without_links_is_none():
    assert keyboard.tickets_kb(1, [(1, None)]) is None


def test_draw_summary_shows_tickets(db):
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="Ann")
    text = middleware.draw_summary(1, draw)
    assert TEXTS["RU"]["draw"]["summary"]["tickets"] in text


def test_m_tickets_lists_participation(db, bot, make_message):
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="5", name="Ann")
    main.m_tickets(make_message(uid=5))

    text = bot.sent[-1][1]
    assert TEXTS["RU"]["menu"]["tickets_title"] in text
    assert f"#{draw.id}" in text


def test_m_tickets_without_participation(db, bot, make_message):
    main.m_tickets(make_message(uid=5))
    assert bot.sent[-1][1] == TEXTS["RU"]["menu"]["no_tickets"]


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
