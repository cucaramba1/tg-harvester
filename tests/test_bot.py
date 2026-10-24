import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from harvester.bot import App
from harvester.cards import draft_card, pilot_card, video_card
from harvester.writer import Writer
from tests.helpers import CHAT_A, make_draft

REL = "ИИ работа/Промпты.md"
NOTE = "# Промпты\n"


class FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(message_id=len(self.sent))


def app_at(s, db, hour=10):
    return App(s, db, Writer(s, db), now=lambda: datetime(2026, 10, 11, hour, 0), pause=0)


def note(s):
    p = s.vault / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(NOTE, "utf-8")
    return p


def cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def test_draft_card_texts(s, db):
    did = make_draft(db, 1, "append", REL, content="x" * 5000)
    d = db.get_draft(did)
    link = "https://t.me/c/1/1"
    text = draft_card(d, db.get_post(d["post_id"]), "Канал А", link)
    assert text.startswith("→ дописать в [[Промпты]]")
    assert text.endswith(f"📌 Канал А · 2026-10-01\n{link}")
    assert len(text) <= 3500
    db.update_draft(did, mode="new", target="Музыка", title="Сведение")
    assert "→ новая заметка в «Музыка»: Сведение" in draft_card(db.get_draft(did), db.get_post(d["post_id"]), "К", link)
    assert "—" not in text


def test_cards_wait_for_hour_and_respect_daily_cap(s, db):
    for i in (1, 2, 3):
        make_draft(db, i, "append", REL)
    bot = FakeBot()
    asyncio.run(app_at(s, db, hour=8).tick(bot))
    assert bot.sent == []
    app = app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 2
    assert [d["status"] for d in db.drafts("sent")] == ["sent", "sent"]
    assert cbs(bot.sent[0][2]["reply_markup"])[:3] == ["ok:1", "no:1", "alt:1"]


def test_ok_writes_then_rollback_restores(s, db):
    p = note(s)
    did = make_draft(db, 1, "append", REL)
    app = app_at(s, db)
    text, kb = app.handle_callback(f"ok:{did}")
    assert "✅ Записано в [[Промпты]]" in text
    rb = cbs(kb)[0]
    assert rb.startswith("rb:")
    bid = rb.split(":")[1]
    text2, kb2 = app.handle_callback(f"rb:{bid}")
    assert text2 is None and cbs(kb2) == [f"rby:{bid}", f"rbn:{bid}"]
    text3, _ = app.handle_callback(f"rby:{bid}")
    assert text3.startswith("↩️ Откатил") and p.read_text("utf-8") == NOTE


def test_ok_on_missing_note_reports_error(s, db):
    did = make_draft(db, 1, "append", "ИИ работа/Нет.md")
    text, kb = app_at(s, db).handle_callback(f"ok:{did}")
    assert "⚠️ Не записал" in text and cbs(kb)[0] == f"ok:{did}"  # кнопки остаются: сменить цель или отклонить


def test_second_ok_tap_does_not_overwrite_written_card(s, db):
    note(s)
    did = make_draft(db, 1, "append", REL)
    app = app_at(s, db)
    text, kb = app.handle_callback(f"ok:{did}")
    assert text and "✅ Записано" in text
    assert app.handle_callback(f"ok:{did}") == (None, None)


def test_no_rejects_draft_and_post(s, db):
    did = make_draft(db, 1, "append", REL)
    text, _ = app_at(s, db).handle_callback(f"no:{did}")
    assert "❌ Отклонено" in text
    d = db.get_draft(did)
    assert d["status"] == "rejected" and db.get_post(d["post_id"])["status"] == "rejected"


def test_alt_and_to_change_target(s, db):
    did = make_draft(db, 1, "append", REL)
    db.update_draft(did, alts=[["append", REL], ["new", "Музыка"]])
    app = app_at(s, db)
    _, kb = app.handle_callback(f"alt:{did}")
    assert cbs(kb) == [f"to:{did}:0", f"to:{did}:1", f"back:{did}"]
    text, kb2 = app.handle_callback(f"to:{did}:1")
    d = db.get_draft(did)
    assert (d["mode"], d["target"], d["title"]) == ("new", "Музыка", "суть поста")
    assert "новая заметка в «Музыка»" in text and cbs(kb2)[0] == f"ok:{did}"


def test_video_buttons(s, db):
    pid = db.add_post(CHAT_A, 1, None, "2026-10-01T00:00:00+00:00", "видео про промпты", "video")
    db.update_post(pid, status="video_ask", video_state="ask")
    bot = FakeBot()
    app = app_at(s, db)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 1 and cbs(bot.sent[0][2]["reply_markup"]) == [f"vy:{pid}", f"vn:{pid}"]
    app.handle_callback(f"vy:{pid}")
    assert db.get_post(pid)["video_state"] == "yes"
    pid2 = db.add_post(CHAT_A, 2, None, "d", "ещё видео", "video")
    db.update_post(pid2, status="video_ask", video_state="asked")
    app.handle_callback(f"vn:{pid2}")
    assert db.get_post(pid2)["status"] == "rejected"


def test_pilot_and_labels(s, db):
    for i in (1, 2):
        pid = db.add_post(CHAT_A, i, None, "2026-10-01T00:00:00+00:00", f"пост {i}", None)
        db.update_post(pid, score=0.5, section_score=0.4)
    bot = FakeBot()
    app = app_at(s, db)
    asyncio.run(app.pilot(bot, n=5))
    assert len(bot.sent) == 2
    app.handle_callback("pl:1:1")
    app.handle_callback("pl:2:0")
    assert sorted(r["useful"] for r in db.labeled()) == [0, 1]


def test_alerts_now_reports_once_after_hour(s, db):
    db.push_alert("💾 мало места")
    rep = {"date": "2026-10-11", "limit": 5, "calls": 2, "queue": 7, "drafted": 3, "skipped": 4, "errors": 0,
           "aborted": None, "retro": True}
    db.kv_set("night_report", json.dumps(rep))
    bot = FakeBot()
    asyncio.run(app_at(s, db, hour=4).tick(bot))
    assert [t for _, t, _ in bot.sent] == ["💾 мало места"]
    app = app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    nights = [t for _, t, _ in bot.sent if t.startswith("🌙")]
    assert len(nights) == 1 and "2/5" in nights[0]


def test_audit_card_and_rollback(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    aid = db.add_audit(bid, "doubt", "не по теме")
    bot = FakeBot()
    app = App(s, db, w, now=lambda: datetime(2026, 10, 11, 10), pause=0)
    asyncio.run(app.tick(bot))
    audit_msgs = [x for x in bot.sent if x[1].startswith("🔍")]
    assert len(audit_msgs) == 1 and cbs(audit_msgs[0][2]["reply_markup"]) == [f"ab:{aid}", f"ak:{aid}"]
    text, _ = app.handle_callback(f"ab:{aid}")
    assert text.startswith("↩️") and p.read_text("utf-8") == NOTE
    assert db.get_audit(aid)["status"] == "rolled_back"


def test_status_text(s, db):
    db.save_channel(CHAT_A, read_mark=1, new_cursor=1, retro_cursor=2, retro_until=None, phase="retro")
    db.add_post(CHAT_A, 1, None, "d", "t", None)
    text = app_at(s, db).status()
    assert "Канал А: ретро" in text and "new: 1" in text


def test_video_and_pilot_cards_put_source_and_link_last(s, db):
    did = make_draft(db, 1, "append", REL)
    post = dict(db.get_post(db.get_draft(did)["post_id"]))
    link = "https://t.me/c/1/1"
    v = video_card(post, "Канал А", link)
    assert v.startswith(post["text"][:20]) and v.endswith(link) and "разобрать видео?" in v
    p = pilot_card({**post, "score": 0.5, "extra_text": None}, "Канал А", link)
    assert p.endswith(link) and not p.startswith("🧪")


def test_video_asks_capped_per_day_best_score_first(s, db):
    for i, sc in ((1, 0.1), (2, 0.9), (3, 0.5)):
        pid = db.add_post(CHAT_A, i, None, "2026-10-01T00:00:00+00:00", f"видео {i}", "video")
        db.update_post(pid, status="video_ask", video_state="ask", score=sc)
    bot = FakeBot()
    app = app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == s.video_asks_per_day == 2  # пока не ответил на видео, вопроса «ещё?» нет
    _answer_asked(app, db)
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 3  # два видео и один вопрос «ещё?»
    assert "видео 2" in bot.sent[0][1] and "видео 3" in bot.sent[1][1]
    assert cbs(bot.sent[2][2]["reply_markup"]) == ["mv:1", "mv:0"]
    assert db.get_post(1)["video_state"] == "ask"


def _answer_asked(app, db):
    """Нажать «Разобрать» на всех выданных видео-карточках."""
    for r in db.c.execute("SELECT id FROM posts WHERE video_state='asked'").fetchall():
        app.handle_callback(f"vy:{r['id']}")


def _three_asks(db):
    for i, sc in ((1, 0.1), (2, 0.9), (3, 0.5)):
        pid = db.add_post(CHAT_A, i, None, "2026-10-01T00:00:00+00:00", f"видео {i}", "video")
        db.update_post(pid, status="video_ask", video_state="ask", score=sc)


def test_more_prompt_yes_sends_next_batch_once_asked(s, db):
    _three_asks(db)
    bot, app = FakeBot(), app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    _answer_asked(app, db)
    for _ in range(3):
        asyncio.run(app.tick(bot))
    assert len(bot.sent) == 3  # вопрос «ещё?» не повторяется
    text, _ = app.handle_callback("mv:1")
    assert "ещё" in text
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 4 and "видео 1" in bot.sent[3][1]  # очередь кончилась, нового вопроса нет
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 4


def test_more_prompt_no_stops_for_the_day(s, db):
    _three_asks(db)
    bot, app = FakeBot(), app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    _answer_asked(app, db)
    asyncio.run(app.tick(bot))
    app.handle_callback("mv:0")
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 3 and db.get_post(1)["video_state"] == "ask"


def test_more_yes_pressed_at_night_sends_immediately(s, db):
    _three_asks(db)
    bot = FakeBot()
    day = app_at(s, db, hour=10)
    asyncio.run(day.tick(bot))
    _answer_asked(day, db)
    asyncio.run(day.tick(bot))  # 2 видео + вопрос «ещё?»
    night = app_at(s, db, hour=3)  # «Да» нажато после полуночи, день ещё закрыт
    night.handle_callback("mv:1")
    asyncio.run(night.tick(bot))
    assert len(bot.sent) == 4 and "видео 1" in bot.sent[3][1]


def test_more_prompt_is_always_the_last_message(s, db):
    _three_asks(db)
    make_draft(db, 10, "append", REL)
    bot, app = FakeBot(), app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))  # видео-карточки и черновик
    _answer_asked(app, db)
    asyncio.run(app.tick(bot))  # вопрос «ещё?» после ответов
    assert cbs(bot.sent[-1][2]["reply_markup"]) == ["mv:1", "mv:0"]
    first_prompt = len(bot.sent)
    make_draft(db, 11, "append", REL)
    asyncio.run(app.tick(bot))  # пришла новая карточка: вопрос переезжает вниз
    assert bot.deleted == [first_prompt]
    assert cbs(bot.sent[-1][2]["reply_markup"]) == ["mv:1", "mv:0"] and len(bot.sent) == first_prompt + 2
    asyncio.run(app.tick(bot))  # ничего нового: вопрос не дублируется
    assert len(bot.sent) == first_prompt + 2 and bot.deleted == [first_prompt]
    app.handle_callback("mv:0")
    db.push_alert("ночной алерт")
    asyncio.run(app.tick(bot))  # после ответа вопрос больше не возвращается
    assert bot.sent[-1][1] == "ночной алерт" and len(bot.sent) == first_prompt + 3


def test_more_prompt_deleted_at_midnight_if_unanswered(s, db):
    _three_asks(db)
    bot, app = FakeBot(), app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    _answer_asked(app, db)
    asyncio.run(app.tick(bot))
    prompt_id = len(bot.sent)
    assert cbs(bot.sent[-1][2]["reply_markup"]) == ["mv:1", "mv:0"]
    next_day = App(s, db, Writer(s, db), now=lambda: datetime(2026, 10, 12, 0, 0), pause=0)
    asyncio.run(next_day.tick(bot))
    assert bot.deleted == [prompt_id] and len(bot.sent) == prompt_id


def test_video_answers_become_labels_with_reason_buttons(s, db):
    app = app_at(s, db)
    yes = db.add_post(CHAT_A, 1, None, "d", "видео про промпты", "video")
    no = db.add_post(CHAT_A, 2, None, "d", "ещё видео", "video")
    db.update_post(yes, status="video_ask", video_state="asked")
    db.update_post(no, status="video_ask", video_state="asked")
    app.handle_callback(f"vy:{yes}")
    text, kb = app.handle_callback(f"vn:{no}")
    lab = {r["post_id"]: (r["useful"], r["source"]) for r in db.c.execute("SELECT * FROM labels")}
    assert lab == {yes: (1, "video"), no: (0, "video")}
    assert cbs(kb) == [f"vr:{no}:1", f"vr:{no}:2", f"vr:{no}:3"]
    text, kb = app.handle_callback(f"vr:{no}:2")
    assert kb is None and "дубль" in text
    assert db.c.execute("SELECT reason FROM labels WHERE post_id=?", (no,)).fetchone()[0] == "дубль"
    assert app.handle_callback(f"vr:{no}:9") == (None, None)  # кривой код причины
    assert app.handle_callback("vr:999:1") == ("Не найдено", None)  # метки нет, пост не трогаем


def test_video_asks_calibration_goes_before_best_score(tmp_path, db):
    from tests.helpers import CHAT_B, make_settings
    s = make_settings(tmp_path, video_calib_n=2, video_calib_channels=(CHAT_B,), video_asks_per_day=2)
    for chat, i, sc in ((CHAT_A, 1, 0.9), (CHAT_A, 2, 0.8), (CHAT_B, 3, 0.2), (CHAT_B, 4, 0.1)):
        pid = db.add_post(chat, i, None, "2026-10-01T00:00:00+00:00", f"видео {i}", "video")
        db.update_post(pid, status="video_ask", video_state="ask", score=sc)
    bot = FakeBot()
    app = app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    texts = [m[1] for m in bot.sent]
    assert len(texts) == 2 and "видео 3" in texts[0] and "видео 4" in texts[1]  # слабые, но из канала без ответов
