import json
from datetime import timedelta

import captcha
import config
import middleware
import models
from models import utcnow
from tool import TEXTS


def _draw(db, **kwargs):
    defaults = dict(owner_id="1", channel_id="-1001", channel_name="Ch",
                    channel_username="ch", text="Prize", file_type="text",
                    status="active", winners_count=1, max_players=0,
                    end_at=utcnow() + timedelta(hours=1), language="RU")
    defaults.update(kwargs)
    return db.new(models.Draw, **defaults)


# ======================= УЧАСТИЕ =======================
def test_join_success(db, bot, make_user):
    draw = _draw(db)
    text, alert = middleware.join_draw_by_id(make_user(700), draw.id)
    assert (text, alert) == (TEXTS["RU"]["post"]["joined"], False)
    assert db.count(models.Player, draw_id=draw.id) == 1


def test_join_twice(db, bot, make_user):
    draw = _draw(db)
    user = make_user(700)
    middleware.join_draw_by_id(user, draw.id)
    text, alert = middleware.join_draw_by_id(user, draw.id)
    assert text == TEXTS["RU"]["post"]["already"] and alert is False
    assert db.count(models.Player, draw_id=draw.id) == 1


def test_join_requires_subscription(db, bot, make_user):
    draw = _draw(db)
    bot.chat_members[("-1001", "700")] = "left"
    text, alert = middleware.join_draw_by_id(make_user(700), draw.id)
    assert alert is True and "Ch" in text
    assert db.count(models.Player, draw_id=draw.id) == 0


def test_join_full(db, bot, make_user):
    draw = _draw(db, max_players=1)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="first")
    text, alert = middleware.join_draw_by_id(make_user(700), draw.id)
    assert (text, alert) == (TEXTS["RU"]["post"]["full"], True)


def test_join_finished_draw(db, bot, make_user):
    draw = _draw(db, status="finished")
    text, alert = middleware.join_draw_by_id(make_user(700), draw.id)
    assert alert is True and text == TEXTS["RU"]["post"]["finished"]


def test_begin_join_without_captcha(db, bot, make_user, monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    draw = _draw(db)
    text, alert = middleware.begin_join(make_user(700), draw.id)
    assert alert is False
    assert db.count(models.Player, draw_id=draw.id) == 1


def test_begin_join_respects_issue_limit(db, bot, make_user, monkeypatch):
    monkeypatch.setattr(captcha, "allow", lambda user_id: False)
    draw = _draw(db)
    text, alert = middleware.begin_join(make_user(700), draw.id)
    assert alert is True
    assert text == TEXTS["RU"]["captcha"]["too_many_attempts"]


# ======================= ПУБЛИКАЦИЯ =======================
def test_publish_sets_message_and_link(db, bot):
    draw = _draw(db, status="scheduled")
    ok, link = middleware.publish_draw(draw.id)
    stored = db.get_one(models.Draw, id=draw.id)
    assert ok is True
    assert stored.status == "active"
    assert stored.message_id is not None
    assert link == f"https://t.me/ch/{stored.message_id}"
    assert any(chat == "-1001" for chat, _, _ in bot.sent)


def test_publish_twice_is_ignored(db, bot):
    draw = _draw(db, status="scheduled")
    assert middleware.publish_draw(draw.id)[0] is True
    assert middleware.publish_draw(draw.id) == (False, None)


def test_publish_failure_rolls_back_to_draft(db, bot):
    draw = _draw(db, status="scheduled")
    bot.fail["send_message"] = RuntimeError("no rights")
    ok, error = middleware.publish_draw(draw.id)
    assert ok is False and "no rights" in error
    assert db.get_one(models.Draw, id=draw.id).status == "draft"


# ======================= ЗАВЕРШЕНИЕ =======================
def test_finish_picks_winners_announces_and_notifies(db, bot):
    draw = _draw(db, winners_count=2)
    for i in range(3):
        db.new(models.Player, draw_id=draw.id, user_id=str(i), name=f"u{i}")

    assert middleware.finish_draw(draw.id) is True

    stored = db.get_one(models.Draw, id=draw.id)
    winners = json.loads(stored.winners)
    assert stored.status == "finished"
    assert len(winners) == 2
    assert bot.pins, "итоги должны закрепляться в канале"
    chats = {str(chat) for chat, _, _ in bot.sent}
    assert "-1001" in chats
    assert "1" in chats
    assert {w["id"] for w in winners} <= chats


def test_finish_twice_does_nothing(db, bot):
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="u")
    assert middleware.finish_draw(draw.id) is True
    assert middleware.finish_draw(draw.id) is False


def test_reroll_logs_previous_winners(db, bot):
    draw = _draw(db, winners_count=1)
    for i in range(3):
        db.new(models.Player, draw_id=draw.id, user_id=str(i), name=f"u{i}")
    middleware.finish_draw(draw.id)
    first = json.loads(db.get_one(models.Draw, id=draw.id).winners)

    assert middleware.finish_draw(draw.id, reroll=True) is True

    stored = db.get_one(models.Draw, id=draw.id)
    history = json.loads(stored.reroll_log)
    assert stored.rerolls == 1
    assert history and history[0]["winners"] == first


def test_cancel_active_draw_updates_post(db, bot):
    draw = _draw(db)
    db.update(models.Draw, {"message_id": 55}, id=draw.id)
    assert middleware.cancel_draw(draw.id) is True
    assert db.get_one(models.Draw, id=draw.id).status == "cancelled"
    assert bot.markups and bot.markups[-1][0] == "-1001"


def test_delete_draw_removes_players(db, bot):
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="u")
    middleware.delete_draw(draw.id)
    assert db.get_one(models.Draw, id=draw.id) is None
    assert db.count(models.Player, draw_id=draw.id) == 0


def test_players_csv(db):
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="Ann", username="ann")
    data, count = middleware.players_csv(draw.id)
    assert count == 1
    text = data.getvalue().decode("utf-8-sig")
    assert "user_id" in text and "ann" in text


# ======================= НАПОМИНАНИЯ / УДАЛЕНИЕ =======================
def test_remind_owner_once(db, bot):
    draw = _draw(db, end_at=utcnow() + timedelta(minutes=30))
    middleware.remind_owner(draw)
    assert db.get_one(models.Draw, id=draw.id).reminded_1h == 1
    sent = len(bot.sent)

    middleware.remind_owner(db.get_one(models.Draw, id=draw.id))

    assert len(bot.sent) == sent


def test_remind_owner_skips_far_future(db, bot):
    draw = _draw(db, end_at=utcnow() + timedelta(days=5))
    middleware.remind_owner(draw)
    assert db.get_one(models.Draw, id=draw.id).reminded_24h == 0
    assert bot.sent == []


def test_delete_user_data(db, bot):
    db.new(models.User, user_id="5", user_name="Ann")
    db.new(models.User, user_id="6", user_name="Bob", referred_by="5")
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="5", name="Ann")
    db.new(models.UserState, user_id="5", state="c_text", data="{}")

    middleware.delete_user_data(5)

    assert db.get_one(models.User, user_id="5") is None
    assert db.get_one(models.UserState, user_id="5") is None
    assert db.count(models.Player, user_id="5") == 0
    assert db.get_one(models.User, user_id="6").referred_by == ""


def test_tick_publishes_due_draws(db, bot):
    draw = _draw(db, status="scheduled", start_at=utcnow() - timedelta(minutes=1))
    middleware.tick()
    assert db.get_one(models.Draw, id=draw.id).status == "active"


def test_tick_finishes_by_time(db, bot):
    draw = _draw(db, end_at=utcnow() - timedelta(seconds=1))
    db.new(models.Player, draw_id=draw.id, user_id="1", name="u")
    middleware.tick()
    assert db.get_one(models.Draw, id=draw.id).status == "finished"


def test_tick_finishes_by_target(db, bot):
    draw = _draw(db, finish_mode="count", max_players=1, end_at=None)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="u")
    middleware.tick()
    assert db.get_one(models.Draw, id=draw.id).status == "finished"
