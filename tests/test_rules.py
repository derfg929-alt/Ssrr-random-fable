from datetime import datetime

import main
import middleware
import models
from models import utcnow
from tool import TEXTS, fmt_dt, parse_local_time

DRAW_TEXTS = TEXTS["RU"]["draw"]


def test_parse_local_time_formats():
    assert parse_local_time("2030-01-02 10:30", 180) == datetime(2030, 1, 2, 7, 30)
    assert parse_local_time("02.01.2030 10:30", 180) == datetime(2030, 1, 2, 7, 30)
    assert parse_local_time("02.01 10:30", 180).month == 1


def test_parse_local_time_rejects_garbage():
    assert parse_local_time("завтра", 180) is None
    assert parse_local_time("", 180) is None


def test_parse_local_time_without_year_rolls_forward():
    parsed = parse_local_time("01.01 10:00", 180)
    assert parsed is not None
    assert parsed > utcnow()


def test_fmt_dt_converts_to_msk():
    assert fmt_dt(datetime(2030, 1, 2, 7, 30), 180) == "2030-01-02 10:30"
    assert fmt_dt(None, 180) == "—"


def test_text_validator_limits(make_message):
    values, error = main.v_text(make_message(text="x" * 4097), DRAW_TEXTS, {"file_type": "text"})
    assert values is None and "4096" in error

    values, error = main.v_text(make_message(text="ok"), DRAW_TEXTS, {"file_type": "text"})
    assert error is None and values["text"] == "ok"


def test_media_caption_limit(make_message):
    msg = make_message(text="x" * 1100, content_type="photo")
    msg.photo = [type("P", (), {"file_id": "f"})()]
    values, error = main.v_media(msg, DRAW_TEXTS, {"text": "x" * 1100})
    assert values is None and "1024" in error


def test_winners_validator_range(make_message):
    assert main.v_winners(make_message(text="0"), DRAW_TEXTS, {})[1] is not None
    assert main.v_winners(make_message(text="501"), DRAW_TEXTS, {})[1] is not None
    values, error = main.v_winners(make_message(text="3"), DRAW_TEXTS, {})
    assert values == {"winners_count": 3} and error is None


def test_start_validator_rejects_past(make_message):
    values, error = main.v_start(make_message(text="01.01.2020 10:00"), DRAW_TEXTS, {"tz": 180})
    assert values is None and error == DRAW_TEXTS["over_time"]


def test_start_validator_accepts_future(make_message):
    values, error = main.v_start(make_message(text="01.01.2030 10:00"), DRAW_TEXTS, {"tz": 180})
    assert error is None and values["start_at"] == datetime(2030, 1, 1, 7, 0)


def test_end_validator_rejects_before_start(make_message):
    ctx = {"tz": 180, "start_at": datetime(2030, 1, 2, 7, 0)}
    values, error = main.v_end(make_message(text="01.01.2030 10:00"), DRAW_TEXTS, ctx)
    assert values is None and error == DRAW_TEXTS["end_before_start"]


def test_button_validator(make_message):
    assert main.v_btn(make_message(text=""), DRAW_TEXTS, {})[1] is not None
    assert main.v_btn(make_message(text="Участвовать"), DRAW_TEXTS, {})[0] == {
        "button_text": "Участвовать"}


def _draw(db, **kwargs):
    defaults = dict(owner_id="1", channel_id="-1001", channel_name="Ch",
                    channel_username="ch", text="Prize", status="active",
                    winners_count=2, max_players=0)
    defaults.update(kwargs)
    return db.new(models.Draw, **defaults)


def test_pick_winners_count(db):
    draw = _draw(db)
    for i in range(5):
        db.new(models.Player, draw_id=draw.id, user_id=str(i), name=f"u{i}")

    total, winners, fallback = middleware.pick_winners(draw.id, 2)

    assert total == 5
    assert len(winners) == 2
    assert len({w["id"] for w in winners}) == 2
    assert fallback is False


def test_pick_winners_more_than_players(db):
    draw = _draw(db)
    db.new(models.Player, draw_id=draw.id, user_id="1", name="only")
    _, winners, _ = middleware.pick_winners(draw.id, 10)
    assert len(winners) == 1


def test_pick_winners_exclude_previous_falls_back(db):
    draw = _draw(db)
    for i in range(2):
        db.new(models.Player, draw_id=draw.id, user_id=str(i), name=f"u{i}")
    _, winners, fallback = middleware.pick_winners(draw.id, 2, exclude_ids=["0", "1"])
    assert len(winners) == 2
    assert fallback is True


def test_pick_winners_without_players(db):
    draw = _draw(db)
    total, winners, fallback = middleware.pick_winners(draw.id, 2)
    assert (total, winners, fallback) == (0, [], False)


def test_tickets_for_counts_refs_and_boosts(db):
    draw = _draw(db, refs_enabled=1, boost_enabled=1, boost_tickets=3)
    player = db.new(models.Player, draw_id=draw.id, user_id="5", boost_count=2)
    assert middleware.tickets_for(draw, player, refs=4) == 1 + 4 + 2 * 3


def test_tickets_for_ignores_disabled_bonuses(db):
    draw = _draw(db)
    player = db.new(models.Player, draw_id=draw.id, user_id="5", boost_count=2)
    assert middleware.tickets_for(draw, player, refs=4) == 1


def test_ref_counts_is_cached_and_updated(db):
    db.new(models.User, user_id="1", user_name="Ann")
    db.new(models.User, user_id="2", user_name="Bob", referred_by="1")
    first = middleware.ref_counts()
    assert first["1"] == 1
    assert middleware.ref_counts() is first

    db.new(models.User, user_id="3", user_name="Cid")
    middleware.register_referral("3", "1")

    assert middleware.ref_counts() is first
    assert first["1"] == 2
    middleware.invalidate_ref_counts()
    assert middleware.ref_counts()["1"] == 2
