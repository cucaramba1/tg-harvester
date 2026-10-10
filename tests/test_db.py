import json

from tests.helpers import CHAT_A


def test_add_post_is_idempotent(db):
    assert db.add_post(CHAT_A, 5, None, "2026-10-01T00:00:00+00:00", "текст", None) is not None
    assert db.add_post(CHAT_A, 5, None, "2026-10-01T00:00:00+00:00", "текст", None) is None
    assert db.counts() == {"new": 1}


def test_album_parts_merge_into_one_post_and_keep_text(db):
    pid = db.add_post(CHAT_A, 12, 777, "d", "", "photo")
    assert db.add_post(CHAT_A, 11, 777, "d", "подпись альбома", "photo") is None
    assert db.add_post(CHAT_A, 11, 777, "d", "подпись альбома", "photo") is None  # повтор после рестарта
    assert db.add_post(CHAT_A, 12, 777, "d", "", "photo") is None
    p = db.get_post(pid)
    assert p["text"] == "подпись альбома"
    assert [m["msg_id"] for m in json.loads(p["media"])] == [12, 11]
    assert db.counts() == {"new": 1}


def test_update_post_serializes_json(db):
    pid = db.add_post(CHAT_A, 1, None, "d", "t", None)
    db.update_post(pid, status="enriched", images=["a.jpg"], top3=[["x.md", 0.5]])
    p = db.get_post(pid)
    assert p["status"] == "enriched"
    assert json.loads(p["images"]) == ["a.jpg"] and json.loads(p["top3"]) == [["x.md", 0.5]]


def test_bump_attempts(db):
    pid = db.add_post(CHAT_A, 1, None, "d", "t", None)
    assert db.bump_attempts(pid) == 1 and db.bump_attempts(pid) == 2


def test_channel_upsert_and_retro_active(db):
    assert db.retro_active() is False
    db.save_channel(CHAT_A, read_mark=5, new_cursor=5, retro_cursor=6, retro_until=None, phase="new")
    db.save_channel(CHAT_A, phase="retro")
    r = db.channel(CHAT_A)
    assert r["phase"] == "retro" and r["new_cursor"] == 5
    assert db.retro_active() is True
    db.save_channel(CHAT_A, phase="done")
    assert db.retro_active() is False


def test_shift_blocks_moves_only_blocks_below_in_same_file(db):
    a = db.add_block(1, "n.md", "append", 10, 20, "x", {})
    b = db.add_block(2, "n.md", "append", 30, 40, "y", {})
    c = db.add_block(3, "o.md", "append", 30, 40, "z", {})
    db.shift_blocks("n.md", 25, 5)
    assert [db.get_block(i)["start"] for i in (a, b, c)] == [10, 35, 30]
    assert db.get_block(b)["stop"] == 45


def test_alerts_pop_once_and_kv(db):
    db.push_alert("a")
    assert db.pop_alerts() == ["a"] and db.pop_alerts() == []
    assert db.kv_get("k", "d") == "d"
    db.kv_set("k", "v")
    assert db.kv_get("k") == "v"
