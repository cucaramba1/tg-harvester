import asyncio
import json

from harvester.topics import fetch_meta, tag_topics, video_msg_id
from tests.helpers import CHAT_A, CHAT_B, FakeClaude, make_settings


def ask(db, chat, msg, text="подпись", media_msg=None):
    pid = db.add_post(chat, msg, None, "2026-10-01T00:00:00+00:00", text, "video")
    db.update_post(pid, status="video_ask", video_state="ask", score=0.7)
    if media_msg is not None:
        db.update_post(pid, media=[{"msg_id": media_msg, "kind": "video"}])
    return pid


class FakeTg:
    def __init__(self, meta):
        self.meta, self.calls = meta, []

    async def messages_meta(self, chat_id, ids):
        self.calls.append((chat_id, list(ids)))
        return {i: self.meta[(chat_id, i)] for i in ids if (chat_id, i) in self.meta}


def test_video_msg_id_prefers_media_over_post_msg(db):
    p = ask(db, CHAT_A, 5, media_msg=9)
    assert video_msg_id(db.get_post(p)) == 9
    q = db.add_post(CHAT_A, 6, None, "d", "t", None)
    assert video_msg_id(db.get_post(q)) is None


def test_fetch_meta_saves_document_id_and_marks_deleted(db):
    a, b, c = ask(db, CHAT_A, 1), ask(db, CHAT_A, 2), ask(db, CHAT_B, 3)
    tg = FakeTg({(CHAT_A, 1): (111, 5000, 14.0), (CHAT_B, 3): (333, 7000, 20.0)})  # поста 2 в Telegram уже нет
    assert asyncio.run(fetch_meta(tg, db, limit=10)) == 3
    assert {c_[0] for c_ in tg.calls} == {CHAT_A, CHAT_B}
    m = db.get_meta(a)
    assert (m["doc_id"], m["size"], m["dur"]) == (111, 5000, 14.0)
    assert db.get_meta(b)["doc_id"] is None                      # исчез: пометили, повторно не спрашиваем
    assert asyncio.run(fetch_meta(tg, db, limit=10)) == 0


def test_tag_topics_normalizes_validates_and_reuses_names(tmp_path, db):
    s = make_settings(tmp_path)
    ids = [ask(db, CHAT_A, i, text=f"пост {i}") for i in range(1, 5)]
    db.set_topic(ids[0], "шея и осанка")
    out = {"items": [{"post_id": ids[1], "topic": "  Шея  и  осанка "}, {"post_id": ids[2], "topic": "сон"},
                     {"post_id": 99999, "topic": "лишнее"}, {"post_id": ids[3], "topic": ""}]}
    router = FakeClaude([out])
    n = asyncio.run(tag_topics(s, db, router, limit=10))
    assert n == 2 and db.topic_of(ids[1]) == "шея и осанка" and db.topic_of(ids[2]) == "сон"
    assert db.topic_of(ids[3]) is None and db.topic_of(99999) is None
    assert "шея и осанка" in router.calls[0][0]                  # существующие темы переданы в запрос
    assert [r["id"] for r in db.queue_without_topic(10)] == [ids[3]]


def test_tag_topics_router_failure_changes_nothing(tmp_path, db):
    from harvester.brain import BrainError
    s = make_settings(tmp_path)
    p = ask(db, CHAT_A, 1)
    assert asyncio.run(tag_topics(s, db, FakeClaude([BrainError("роутер недоступен")]), limit=10)) == 0
    assert db.topic_of(p) is None


def test_topic_overview_counts_queue_and_unique_files(db):
    a, b, c, d = (ask(db, CHAT_A, i) for i in range(1, 5))
    for pid in (a, b, c):
        db.set_topic(pid, "шея")
    db.set_topic(d, "сон")
    db.save_meta(a, 111, 10, 5.0)
    db.save_meta(b, 111, 10, 5.0)   # тот же файл, другой текст
    db.save_meta(c, 222, 10, 6.0)
    ov = {r["topic"]: (r["n"], r["files"]) for r in db.topic_overview()}
    assert ov == {"шея": (3, 2), "сон": (1, 1)}
    db.update_post(c, video_state="asked")                         # уже отвечено: из очереди тем уходит
    assert {r["topic"]: r["n"] for r in db.topic_overview()} == {"шея": 2, "сон": 1}


# --- пачка 2: карточка темы в боте

from tests.test_bot import FakeBot, app_at, cbs  # noqa: E402


def topical(tmp_path, **over):
    return make_settings(tmp_path, video_by_topic=True, topic_cards_per_day=2, topic_min_size=2, **over)


def seed(db, topic, n, chat=CHAT_A, base=0, score=0.7):
    ids = []
    for i in range(n):
        p = ask(db, chat, base + i + 1, text=f"{topic} видео {i}")
        db.update_post(p, score=score - i / 100)
        db.set_topic(p, topic)
        ids.append(p)
    return ids


def test_topic_card_sent_once_with_three_buttons_and_counts(tmp_path, db):
    s = topical(tmp_path)
    ids = seed(db, "шея и осанка", 3)
    db.save_meta(ids[0], 111, 1, 1.0); db.save_meta(ids[1], 111, 1, 1.0); db.save_meta(ids[2], 222, 1, 1.0)
    bot = FakeBot()
    app = app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    cards = [m for m in bot.sent if "шея и осанка" in m[1]]
    assert len(cards) == 1
    tid = db.topic_state_id("шея и осанка")
    assert cbs(cards[0][2]["reply_markup"]) == [f"tc:{tid}", f"t3:{tid}", f"tx:{tid}"]
    assert "3 видео" in cards[0][1] and "повтор" in cards[0][1]    # 3 поста, 2 разных файла
    assert db.topic_state_of("шея и осанка") == "asked"


def test_topic_cards_capped_per_day_biggest_first_and_small_topics_skipped(tmp_path, db):
    s = topical(tmp_path)
    seed(db, "сон", 2, base=0)
    seed(db, "колени", 5, base=10)
    seed(db, "зрение", 3, base=20)
    seed(db, "одиночка", 1, base=30)
    bot = FakeBot()
    asyncio.run(app_at(s, db, hour=10).tick(bot))
    titles = [m[1] for m in bot.sent if m[1].startswith("🧩")]
    assert len(titles) == 2 and "колени" in titles[0] and "зрение" in titles[1]   # лимит 2 в день, сон и одиночка ждут


def test_topic_managed_posts_are_not_asked_one_by_one(tmp_path, db):
    s = topical(tmp_path, video_asks_per_day=5)
    seed(db, "шея", 3, base=0)
    lone = ask(db, CHAT_A, 99, text="одиночное видео")
    db.set_topic(lone, "одиночка")
    untagged = ask(db, CHAT_A, 100, text="без темы пока")
    bot = FakeBot()
    asyncio.run(app_at(s, db, hour=10).tick(bot))
    video_cards = [m for m in bot.sent if "разобрать видео" in m[1]]
    assert len(video_cards) == 1 and "одиночное видео" in video_cards[0][1]
    assert db.get_post(untagged)["video_state"] == "ask"


def test_close_topic_rejects_all_posts_without_labels(tmp_path, db):
    s = topical(tmp_path)
    ids = seed(db, "шея", 3)
    app = app_at(s, db)
    asyncio.run(app.tick(FakeBot()))
    tid = db.topic_state_id("шея")
    text, kb = app.handle_callback(f"tx:{tid}")
    assert "закрыта" in text and kb is None and "3" in text
    for p in ids:
        r = db.get_post(p)
        assert (r["status"], r["video_state"], r["reason"]) == ("rejected", "no", "тема: не надо")
    assert db.c.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0   # решение по теме не учит вкус по подписям
    assert db.topic_state_of("шея") == "closed"


def test_check_topic_marks_state_for_night_job(tmp_path, db):
    s = topical(tmp_path)
    seed(db, "шея", 2)
    app = app_at(s, db)
    asyncio.run(app.tick(FakeBot()))
    tid = db.topic_state_id("шея")
    text, kb = app.handle_callback(f"tc:{tid}")
    assert "ночью" in text and kb is None and db.topic_state_of("шея") == "check"


def test_show_three_sends_distinct_files_as_ordinary_video_cards(tmp_path, db):
    s = topical(tmp_path)
    ids = seed(db, "шея", 5)
    for pid, doc in zip(ids, (1, 1, 2, 3, 4)):
        db.save_meta(pid, doc, 1, 1.0)           # первые два поста один и тот же файл
    bot = FakeBot()
    app = app_at(s, db)
    asyncio.run(app.tick(bot))
    tid = db.topic_state_id("шея")
    text, kb = app.handle_callback(f"t3:{tid}")
    assert cbs(kb) == [f"tc:{tid}", f"tx:{tid}"]            # кнопка «показать 3» заменяется, остальные остаются
    before = len(bot.sent)
    asyncio.run(app.tick(bot))
    shown = [m for m in bot.sent[before:] if "разобрать видео" in m[1]]
    assert len(shown) == 3
    docs = {db.get_meta(p)["doc_id"] for p in ids if db.get_post(p)["video_state"] == "asked"}
    assert len(docs) == 3                                    # три разных файла, не повторы
    assert cbs(shown[0][2]["reply_markup"])[0].startswith("vy:")
