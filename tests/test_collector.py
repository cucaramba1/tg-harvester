import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from harvester.collector import Collector, media_kind
from tests.helpers import CHAT_A, CHAT_B, make_settings

UTC = timezone.utc


def msg(i, when, text="", grouped=None, kind=None):
    m = SimpleNamespace(id=i, date=when, message=text, grouped_id=grouped,
                        photo=None, voice=None, video=None, video_note=None, audio=None)
    if kind:
        setattr(m, kind, object())
    return m


def d(y, mo, day):
    return datetime(y, mo, day, tzinfo=UTC)


class FakeTg:
    def __init__(self, msgs, marks):
        self.msgs, self.marks = msgs, marks

    async def read_mark(self, chat_id):
        return self.marks[chat_id]

    async def iter_messages(self, chat_id, limit, min_id=0, max_id=0, reverse=False):
        ms = sorted(self.msgs[chat_id], key=lambda m: m.id, reverse=not reverse)
        if min_id:
            ms = [m for m in ms if m.id > min_id]
        if max_id:
            ms = [m for m in ms if m.id < max_id]
        for m in ms[:limit]:
            yield m

    async def first_before(self, chat_id, when):
        older = [m.id for m in self.msgs[chat_id] if m.date < when]
        return max(older) if older else None


def run_all(c, max_steps=50):
    asyncio.run(c.init())
    n = 0
    while asyncio.run(c.step()):
        n += 1
        assert n < max_steps
    return n


def ids(db, chat):
    return sorted(r["msg_id"] for r in db.c.execute("SELECT msg_id FROM posts WHERE chat_id=?", (chat,)))


A = [msg(1, d(2025, 9, 1), "старый"), msg(2, d(2025, 10, 1), "до границы"), msg(3, d(2025, 10, 15), "ретро 3"),
     msg(4, d(2026, 1, 1), "ретро 4"), msg(5, d(2026, 5, 1), "ретро 5"), msg(6, d(2026, 9, 1), "новый 6"),
     msg(7, d(2026, 9, 2), "новый 7"), msg(8, d(2026, 9, 3), "новый 8")]
B = [msg(1, d(2026, 1, 1)), msg(2, d(2026, 2, 1)), msg(3, d(2026, 9, 1), "б3"), msg(4, d(2026, 9, 2), "б4")]


def test_media_kind():
    assert media_kind(msg(1, d(2026, 1, 1), kind="photo")) == "photo"
    assert media_kind(msg(1, d(2026, 1, 1), kind="video_note")) == "video"
    assert media_kind(msg(1, d(2026, 1, 1))) is None


def test_new_phase_covers_all_channels_before_any_retro(s, db):
    c = Collector(s, db, FakeTg({CHAT_A: A, CHAT_B: B}, {CHAT_A: 5, CHAT_B: 2}))
    asyncio.run(c.init())
    for _ in range(3):
        asyncio.run(c.step())
    assert db.channel(CHAT_A)["phase"] == "retro"
    assert db.channel(CHAT_B)["phase"] == "done"  # backfill: false
    assert ids(db, CHAT_A) == [6, 7, 8] and ids(db, CHAT_B) == [3, 4]


def test_retro_stops_at_boundary(s, db):
    run_all(Collector(s, db, FakeTg({CHAT_A: A, CHAT_B: B}, {CHAT_A: 5, CHAT_B: 2})))
    assert ids(db, CHAT_A) == [3, 4, 5, 6, 7, 8]
    assert db.channel(CHAT_A)["phase"] == "done" and db.channel(CHAT_A)["retro_until"] == "2025-10-10"
    assert db.retro_active() is False


def test_preflight_cap_cuts_retro_to_fallback_window(tmp_path, db):
    s = make_settings(tmp_path, retro_cap=1, retro_fallback_days=60)
    a = [msg(1, d(2025, 11, 1), "1"), msg(2, d(2026, 3, 1), "2"), msg(3, d(2026, 8, 1), "3"),
         msg(4, d(2026, 9, 1), "4"), msg(5, d(2026, 9, 15), "5"), msg(6, d(2026, 10, 1), "6")]
    run_all(Collector(s, db, FakeTg({CHAT_A: a, CHAT_B: []}, {CHAT_A: 6, CHAT_B: 0}), today=lambda: date(2026, 10, 10)))
    assert ids(db, CHAT_A) == [4, 5, 6]
    assert db.channel(CHAT_A)["retro_until"] == "2026-08-11"
    assert any("урезано" in t for t in db.pop_alerts())
    assert json.loads(db.kv_get(f"preflight:{CHAT_A}"))["until"] == "2026-08-11"


def test_zero_read_mark_does_not_crawl_years(s, db):
    a = [msg(1, d(2024, 1, 1), "давно"), msg(2, d(2025, 6, 1), "давно 2"), msg(3, d(2025, 11, 1), "свежий")]
    run_all(Collector(s, db, FakeTg({CHAT_A: a, CHAT_B: []}, {CHAT_A: 0, CHAT_B: 0})))
    assert ids(db, CHAT_A) == [3]


def test_album_from_history_becomes_one_post(s, db):
    a = [msg(7, d(2026, 9, 1), "подпись", grouped=99, kind="photo"), msg(8, d(2026, 9, 1), "", grouped=99, kind="photo")]
    run_all(Collector(s, db, FakeTg({CHAT_A: a, CHAT_B: []}, {CHAT_A: 6, CHAT_B: 0})))
    rows = db.c.execute("SELECT * FROM posts").fetchall()
    assert len(rows) == 1 and rows[0]["text"] == "подпись"
    assert [m["msg_id"] for m in json.loads(rows[0]["media"])] == [7, 8]


def test_restart_mid_phase_does_not_duplicate(s, db):
    tg = FakeTg({CHAT_A: A, CHAT_B: B}, {CHAT_A: 5, CHAT_B: 2})
    c = Collector(s, db, tg)
    asyncio.run(c.init())
    asyncio.run(c.step())
    db.save_channel(CHAT_A, new_cursor=5)  # как будто упали до сохранения курсора
    c2 = Collector(s, db, tg)
    asyncio.run(c2.init())  # канал уже есть, init его не трогает
    asyncio.run(c2.step())
    assert ids(db, CHAT_A) == [6, 7, 8]
    assert db.counts() == {"new": 3}


def test_live_message_is_stored_with_source_live(s, db):
    c = Collector(s, db, FakeTg({CHAT_A: [], CHAT_B: []}, {CHAT_A: 0, CHAT_B: 0}))
    asyncio.run(c.on_new(SimpleNamespace(chat_id=CHAT_A, message=msg(50, d(2026, 10, 10), "живой", kind="photo"))))
    row = db.c.execute("SELECT * FROM posts").fetchone()
    assert (row["msg_id"], row["source"], json.loads(row["media"])[0]["kind"]) == (50, "live", "photo")
