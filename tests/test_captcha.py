import time

import captcha


def test_make_returns_png_and_answer():
    item = captcha.make(7)
    assert item["png"].startswith(b"\x89PNG")
    assert item["draw_id"] == 7
    assert 0 <= item["answer"] < 4
    assert item["tries"] == 0
    assert len(item["nonce"]) == 8


def test_save_get_drop():
    item = captcha.make(1)
    captcha.save(5, item, 5, 77)
    saved = captcha.get(5)
    assert saved["message_id"] == 77
    captcha.drop(5)
    assert captcha.get(5) is None


def test_expired_item_is_removed_on_get():
    item = captcha.make(1)
    captcha.save(5, item, 5, 77)
    captcha._items["5"]["expires"] = time.time() - 1
    assert captcha.get(5) is None
    assert "5" not in captcha._items


def test_sweep_removes_abandoned_items_and_pending():
    item = captcha.make(1)
    captcha.save(5, item, 5, 77)
    captcha._items["5"]["expires"] = time.time() - 1
    captcha.set_pending(6, 1)
    captcha._pending["6"] = (1, time.time() - 1)

    captcha.sweep()

    assert captcha._items == {}
    assert captcha._pending == {}


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(captcha, "MAX_ITEMS", 2)
    item = captcha.make(1)
    for uid in (1, 2, 3):
        captcha.save(uid, dict(item), uid, uid)
    assert len(captcha._items) == 2


def test_issue_limit(monkeypatch):
    monkeypatch.setattr(captcha, "MAX_ISSUE", 3)
    assert all(captcha.allow(9) for _ in range(3))
    assert captcha.allow(9) is False
    assert captcha.allow(10) is True


def test_issue_limit_expires_with_window(monkeypatch):
    monkeypatch.setattr(captcha, "MAX_ISSUE", 1)
    assert captcha.allow(9) is True
    assert captcha.allow(9) is False
    monkeypatch.setattr(captcha, "ISSUE_WINDOW", 0)
    captcha.sweep()
    assert captcha.allow(9) is True


def test_pending_roundtrip():
    captcha.set_pending(5, 3)
    assert captcha.pop_pending(5) == 3
    assert captcha.pop_pending(5) is None


def test_forget_clears_everything():
    item = captcha.make(1)
    captcha.save(5, item, 5, 1)
    captcha.set_pending(5, 1)
    captcha.allow(5)

    captcha.forget(5)

    assert captcha.get(5) is None
    assert captcha.pop_pending(5) is None
    assert "5" not in captcha._issued
