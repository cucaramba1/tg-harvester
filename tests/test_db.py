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


def _old_labels_db(path):
    import sqlite3
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE labels(post_id INTEGER PRIMARY KEY, useful INTEGER NOT NULL)")
    c.execute("INSERT INTO labels VALUES(7, 1)")
    c.commit()
    c.close()


def test_labels_migrate_keeps_old_rows(tmp_path):
    from harvester.db import DB
    p = tmp_path / "old.db"
    _old_labels_db(p)
    db = DB(p)
    row = db.c.execute("SELECT * FROM labels WHERE post_id=7").fetchone()
    assert row["useful"] == 1 and row["source"] == "import" and row["reason"] is None
    DB(p)  # второй запуск не падает на уже добавленных колонках


def test_add_label_keeps_reason_and_source(db):
    db.add_label(1, False, source="video")
    db.set_label_reason(1, "дубль")
    db.add_label(1, False, source="video")
    r = db.c.execute("SELECT * FROM labels WHERE post_id=1").fetchone()
    assert (r["useful"], r["source"], r["reason"]) == (0, "video", "дубль") and r["ts"]


def test_backfill_video_labels_from_video_state(db):
    a = db.add_post(CHAT_A, 1, None, "d", "t", "video")
    b = db.add_post(CHAT_A, 2, None, "d", "t", "video")
    c = db.add_post(CHAT_A, 3, None, "d", "t", "video")
    d = db.add_post(CHAT_A, 4, None, "d", "t", "video")
    db.update_post(a, video_state="yes")
    db.update_post(b, video_state="no")
    db.update_post(c, video_state="done")
    db.update_post(d, video_state="ask")
    db.add_label(c, False, source="pilot")  # уже размечено, не перетираем
    assert db.backfill_video_labels() == 2
    got = {r["post_id"]: (r["useful"], r["source"]) for r in db.c.execute("SELECT * FROM labels")}
    assert got == {a: (1, "video"), b: (0, "video"), c: (0, "pilot")}
    assert db.backfill_video_labels() == 0
