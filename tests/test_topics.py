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
