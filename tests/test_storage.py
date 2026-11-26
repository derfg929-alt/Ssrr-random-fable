import json
import os

import pytest

import config
import models
from base import DataBase, IntegrityError


def make_db(tmp_path):
    return DataBase(str(tmp_path / "bot.json"))


def test_new_and_reload_replays_journal(tmp_path):
    db = make_db(tmp_path)
    db.new(models.User, user_id="1", user_name="Ann")
    db.update(models.User, {"language": "ENG"}, user_id="1")
    assert os.path.exists(db.journal)

    again = DataBase(str(tmp_path / "bot.json"))
    user = again.get_one(models.User, user_id="1")
    assert user is not None
    assert user.language == "ENG"


def test_delete_and_upsert_are_replayed(tmp_path):
    db = make_db(tmp_path)
    db.new(models.User, user_id="1", user_name="Ann")
    db.new(models.User, user_id="2", user_name="Bob")
    db.delete(models.User, user_id="1")
    db.upsert(models.UserState, user_id="2", state="c_text", data="{}")

    again = DataBase(str(tmp_path / "bot.json"))
    assert again.get_one(models.User, user_id="1") is None
    assert again.get_one(models.User, user_id="2").user_name == "Bob"
    assert again.get_one(models.UserState, user_id="2").state == "c_text"


def test_compaction_writes_snapshot_and_truncates_journal(tmp_path):
    db = make_db(tmp_path)
    db.new(models.User, user_id="1", user_name="Ann")
    db.save()

    with open(db.path, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["users"][0]["user_id"] == "1"
    assert "__seq__" in raw
    assert os.path.getsize(db.journal) == 0


def test_compaction_keeps_recent_data_after_reload(tmp_path):
    db = make_db(tmp_path)
    db.new(models.User, user_id="1", user_name="Ann")
    db.save()
    db.new(models.User, user_id="2", user_name="Bob")
    again = DataBase(str(tmp_path / "bot.json"))
    assert again.count(models.User) == 2


def test_compaction_every_n_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOURNAL_COMPACT_OPS", 2)
    db = make_db(tmp_path)
    db.new(models.User, user_id="1", user_name="Ann")
    db.new(models.User, user_id="2", user_name="Bob")
    assert os.path.getsize(db.journal) == 0
    with open(db.path, encoding="utf-8") as f:
        assert len(json.load(f)["users"]) == 2


def test_unique_violation(tmp_path):
    db = make_db(tmp_path)
    db.new(models.Player, draw_id=1, user_id="1")
    with pytest.raises(IntegrityError):
        db.new(models.Player, draw_id=1, user_id="1")


def test_ids_are_not_reused_after_delete(tmp_path):
    db = make_db(tmp_path)
    first = db.new(models.Draw, owner_id="1")
    second = db.new(models.Draw, owner_id="1")
    db.delete(models.Draw, id=second.id)
    third = db.new(models.Draw, owner_id="1")
    assert (first.id, second.id, third.id) == (1, 2, 3)


def test_guard_blocks_insert_atomically(tmp_path):
    db = make_db(tmp_path)
    db.new(models.Player, draw_id=1, user_id="1")
    created = db.new(models.Player, _guard=lambda: db.count(models.Player, draw_id=1) < 1,
                     draw_id=1, user_id="2")
    assert created is None
    assert db.count(models.Player, draw_id=1) == 1


def test_broken_journal_line_is_skipped(tmp_path):
    db = make_db(tmp_path)
    db.new(models.User, user_id="1", user_name="Ann")
    with open(db.journal, "a", encoding="utf-8") as f:
        f.write('{"o": "n", "t": "users", "r": {"user_id": "2"')  # обрыв записи
        f.write("\n")
        f.write("мусор\n")
    again = DataBase(str(tmp_path / "bot.json"))
    assert again.get_one(models.User, user_id="1").user_name == "Ann"
    assert again.count(models.User) == 1


def test_replayed_insert_is_idempotent(tmp_path):
    """Повтор вставки после компакции не должен дублировать строку."""
    db = make_db(tmp_path)
    db.new(models.User, user_id="1", user_name="Ann")
    db.save()
    # имитируем падение между записью снимка и очисткой журнала
    with open(db.journal, "a", encoding="utf-8") as f:
        f.write(json.dumps({"o": "n", "t": "users",
                            "r": {"user_id": "1", "user_name": "Ann"}}) + "\n")
    again = DataBase(str(tmp_path / "bot.json"))
    assert again.count(models.User) == 1


def test_replace_all(tmp_path):
    db = make_db(tmp_path)
    db.new(models.Draw, owner_id="1")
    db.replace_all({"users": [{"user_id": "7", "user_name": "Seven"}],
                    "draws": []})
    assert db.count(models.Draw) == 0
    assert db.get_one(models.User, user_id="7").user_name == "Seven"
    assert DataBase(str(tmp_path / "bot.json")).count(models.User) == 1


def test_corrupt_snapshot_is_moved_aside(tmp_path):
    path = tmp_path / "bot.json"
    path.write_text("{ not json", encoding="utf-8")
    db = DataBase(str(path))
    assert db.count(models.User) == 0
    assert any(p.name.startswith("bot.json.corrupt-") for p in tmp_path.iterdir())
