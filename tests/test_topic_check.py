import asyncio
import json
from datetime import datetime

from harvester.brain import BrainError
from harvester.topic_check import run_topic_checks
from tests.helpers import CHAT_A, FakeClaude, make_settings
from tests.test_topics import ask

NIGHT = lambda: datetime(2026, 10, 25, 3, 0)   # noqa: E731
NOON = lambda: datetime(2026, 10, 25, 12, 0)   # noqa: E731


class FakeTg:
    def __init__(self):
        self.downloaded = []

    async def download(self, chat_id, msg_id, dest):
        self.downloaded.append(msg_id)
        p = dest / f"{msg_id}.mp4"
        p.write_bytes(b"x")
        return p


def make_world(tmp_path, db, docs, **over):
    """Тема «шея» в состоянии check: по посту на каждый элемент docs (одинаковые docs = один и тот же файл)."""
    s = make_settings(tmp_path, video_by_topic=True, topic_check_per_night=over.pop("per_night", 10), **over)
    ids = []
    for i, doc in enumerate(docs):
        p = ask(db, CHAT_A, i + 1, text=f"подпись {i + 1}")
        db.set_topic(p, "шея")
        db.save_meta(p, doc, 100, 10.0)
        ids.append(p)
    tid = db.ensure_topic_state("шея")
    db.set_topic_state(tid, "check")
    return s, ids, tid


def tools(speech="голос", ocr="надпись", loud=-20.0, foreign=False, calls=None):
    calls = calls if calls is not None else {"stt": 0, "ocr": 0}

    async def extract(video, out):
        a = out / "audio.wav"; a.write_bytes(b"a" * 2000)
        f = out / "frame_1.jpg"; f.write_bytes(b"f")
        return a, [f]

    async def stt(path):
        calls["stt"] += 1
        return speech

    def ocr_fn(path):
        calls["ocr"] += 1
        return ocr

    return dict(extract=extract, stt=stt, ocr=ocr_fn, loudness=lambda p: loud, foreign_whisper=lambda: foreign,
                disk=lambda: 100.0), calls


def merged(*groups, unclear=()):
    return {"groups": list(groups), "unclear": list(unclear)}


def test_only_in_night_window(tmp_path, db):
    s, ids, tid = make_world(tmp_path, db, [1, 2])
    t, calls = tools()
    tg = FakeTg()
    asyncio.run(run_topic_checks(s, db, tg, FakeClaude([]), now=NOON, **t))
    assert tg.downloaded == [] and db.topic_state_of("шея") == "check"


def test_one_download_per_unique_file_and_report_after_merge(tmp_path, db):
    s, ids, tid = make_world(tmp_path, db, [1, 1, 2])         # первые два поста один и тот же файл
    t, calls = tools()
    tg = FakeTg()
    router = FakeClaude([lambda p: merged({"title": "наклоны шеи", "note": "10 повторов", "post_ids": [ids[0], ids[2]]})])
    asyncio.run(run_topic_checks(s, db, tg, router, now=NIGHT, **t))
    assert len(tg.downloaded) == 2 and calls["stt"] == 2
    assert db.topic_state_of("шея") == "checked"
    rep = json.loads(db.topic_by_id(tid)["report"])
    assert rep["total"] == 3 and rep["files"] == 2
    g = rep["groups"][0]
    assert g["title"] == "наклоны шеи" and g["count"] == 3 and rep["undescribed"] == []   # повтор добавляется к группе


def test_silent_audio_skips_whisper_and_empty_clip_is_undescribed(tmp_path, db):
    s, ids, tid = make_world(tmp_path, db, [1])
    t, calls = tools(speech="Продолжение следует...", ocr="", loud=-91.0)
    asyncio.run(run_topic_checks(s, db, FakeTg(), FakeClaude([]), now=NIGHT, **t))
    assert calls["stt"] == 0                                    # тишина: Whisper не зовём, мусор не получаем
    row = db.c.execute("SELECT * FROM topic_checks WHERE post_id=?", (ids[0],)).fetchone()
    assert row["silent"] == 1 and row["speech"] == ""
    rep = json.loads(db.topic_by_id(tid)["report"])             # описанных нет, роутер не нужен
    assert rep["groups"] == [] and [u["post_id"] for u in rep["undescribed"]] == [ids[0]]


def test_budget_per_night_and_resume_next_night(tmp_path, db):
    s, ids, tid = make_world(tmp_path, db, [1, 2, 3], per_night=2)
    t, calls = tools()
    tg = FakeTg()
    router = FakeClaude([lambda p: merged({"title": "а", "note": "", "post_ids": ids})])
    asyncio.run(run_topic_checks(s, db, tg, router, now=NIGHT, **t))
    assert len(tg.downloaded) == 2 and db.topic_state_of("шея") == "check"
    asyncio.run(run_topic_checks(s, db, tg, router, now=lambda: datetime(2026, 10, 26, 3, 0), **t))
    assert len(tg.downloaded) == 3 and db.topic_state_of("шея") == "checked"


def test_foreign_whisper_makes_job_wait_without_losing_anything(tmp_path, db):
    s, ids, tid = make_world(tmp_path, db, [1])
    t, calls = tools(foreign=True)
    tg = FakeTg()
    asyncio.run(run_topic_checks(s, db, tg, FakeClaude([]), now=NIGHT, **t))
    assert tg.downloaded == [] and db.topic_state_of("шея") == "check"


def test_router_failure_keeps_topic_in_check_and_does_not_recheck_files(tmp_path, db):
    s, ids, tid = make_world(tmp_path, db, [1, 2])
    t, calls = tools()
    tg = FakeTg()
    asyncio.run(run_topic_checks(s, db, tg, FakeClaude([BrainError("роутер недоступен")]), now=NIGHT, **t))
    assert db.topic_state_of("шея") == "check" and len(tg.downloaded) == 2
    router = FakeClaude([lambda p: merged({"title": "а", "note": "", "post_ids": ids})])
    asyncio.run(run_topic_checks(s, db, tg, router, now=NIGHT, **t))
    assert len(tg.downloaded) == 2 and db.topic_state_of("шея") == "checked"   # файлы второй раз не качали


def test_posts_missing_from_router_answer_become_own_groups(tmp_path, db):
    s, ids, tid = make_world(tmp_path, db, [1, 2, 3])
    t, calls = tools()
    router = FakeClaude([lambda p: merged({"title": "а", "note": "", "post_ids": [ids[0], 99999]})])
    asyncio.run(run_topic_checks(s, db, FakeTg(), router, now=NIGHT, **t))
    rep = json.loads(db.topic_by_id(tid)["report"])
    covered = sorted(i for g in rep["groups"] for i in g["posts"])
    assert covered == sorted(ids) and 99999 not in covered
