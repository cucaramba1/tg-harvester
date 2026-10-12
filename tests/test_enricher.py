import asyncio
import json
from pathlib import Path

from harvester.enricher import Enricher
from tests.helpers import CHAT_A

LONG_OCR = "текст с картинки про промпты для claude и нейросети"


class FakeTg:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def download(self, chat_id, msg_id, dest):
        if self.fail:
            raise RuntimeError("сеть упала")
        self.calls.append(msg_id)
        p = Path(dest) / f"{msg_id}.bin"
        p.write_bytes(b"x")
        return p


async def fake_stt(path):
    assert path.exists()
    return "расшифровка голосового про бег и колени"


def enr(s, db, tg=None, ocr=lambda p: LONG_OCR, disk=lambda: 50.0, extract=None):
    return Enricher(s, db, tg or FakeTg(), ocr=ocr, stt=fake_stt, disk=disk, extract=extract)


def post(db, msg_id, text, kind=None):
    pid = db.add_post(CHAT_A, msg_id, None, "2026-10-01T00:00:00+00:00", text, kind)
    return db.get_post(pid)


def test_ad_is_filtered_before_any_download(s, db):
    tg = FakeTg()
    p = post(db, 1, "Розыгрыш iPhone, подпишись", "photo")
    assert asyncio.run(enr(s, db, tg).process(p)) == "filtered"
    assert tg.calls == [] and db.get_post(p["id"])["reason"] == "реклама: розыгрыш"


def test_photo_is_ocred_and_kept(s, db):
    p = post(db, 2, "Смотри", "photo")
    assert asyncio.run(enr(s, db).process(p)) == "enriched"
    row = db.get_post(p["id"])
    assert row["extra_text"] == LONG_OCR
    imgs = json.loads(row["images"])
    assert len(imgs) == 1 and Path(imgs[0]).exists() and str(s.keep_dir) in imgs[0]


def test_short_post_without_ocr_is_filtered_and_keep_removed(s, db):
    p = post(db, 3, "Шедевр", "photo")
    assert asyncio.run(enr(s, db, ocr=lambda p: "").process(p)) == "filtered"
    assert db.get_post(p["id"])["reason"] == "short"
    assert not (s.keep_dir / str(p["id"])).exists()


def test_low_disk_skips_media_and_alerts_once_a_day(s, db):
    tg = FakeTg()
    e = enr(s, db, tg, disk=lambda: 1.0)
    p1 = post(db, 4, "Длинный пост про промпты для нейросетей без картинки", "photo")
    p2 = post(db, 5, "Ещё один длинный пост про промпты и нейросети", "photo")
    assert asyncio.run(e.process(p1)) == "enriched"
    assert asyncio.run(e.process(p2)) == "enriched"
    assert tg.calls == []
    assert len([t for t in db.pop_alerts() if "диске" in t]) == 1


def test_voice_is_transcribed_and_temp_removed(s, db):
    p = post(db, 6, "", "voice")
    assert asyncio.run(enr(s, db).process(p)) == "enriched"
    assert db.get_post(p["id"])["extra_text"] == "расшифровка голосового про бег и колени"
    assert list(s.tmp_dir.iterdir()) == []


def test_errors_retry_then_mark_error(s, db):
    e = enr(s, db, FakeTg(fail=True))
    p = post(db, 7, "Длинный пост с картинкой про нейросети", "photo")
    assert asyncio.run(e.process(p)) == "new"
    assert asyncio.run(e.process(db.get_post(p["id"]))) == "new"
    assert asyncio.run(e.process(db.get_post(p["id"]))) == "error"
    assert db.get_post(p["id"])["status"] == "error"


def test_analyze_video_adds_transcript_and_frames(s, db):
    async def extract(video, out):
        audio = Path(out) / "a.wav"
        audio.write_bytes(b"w")
        frame = Path(out) / "frame_1.jpg"
        frame.write_bytes(b"f")
        return audio, [frame]

    p = post(db, 8, "Видео про промпты", "video")
    db.update_post(p["id"], status="video_ask", video_state="yes", extra_text="было")
    asyncio.run(enr(s, db, extract=extract).analyze_video(db.get_post(p["id"])))
    row = db.get_post(p["id"])
    assert row["status"] == "enriched" and row["video_state"] == "done"
    assert row["extra_text"] == f"было\n\nрасшифровка голосового про бег и колени\n\n{LONG_OCR}"


def test_analyze_video_failure_does_not_get_stuck(s, db):
    p = post(db, 9, "Видео про промпты", "video")
    db.update_post(p["id"], status="video_ask", video_state="yes")
    asyncio.run(enr(s, db, FakeTg(fail=True)).analyze_video(db.get_post(p["id"])))
    row = db.get_post(p["id"])
    assert (row["status"], row["video_state"]) == ("enriched", "done")
    assert any("видео" in t for t in db.pop_alerts())


class VideoTg(FakeTg):
    """Тип медиа «photo», а Telegram отдаёт видео (превью ссылки): так падал живой OCR."""
    async def download(self, chat_id, msg_id, dest):
        p = Path(dest) / f"{msg_id}.mp4"
        p.write_bytes(b"x")
        return p


def _boom(path):
    raise ValueError("cannot identify image file")


def test_photo_kind_with_video_file_keeps_post_text(s, db):
    p = post(db, 4, "Длинный нормальный текст про промпты для claude и нейросети", "photo")
    assert asyncio.run(enr(s, db, VideoTg(), ocr=_boom).process(p)) == "enriched"
    row = db.get_post(p["id"])
    assert row["status"] == "enriched" and json.loads(row["images"]) == []


def test_ocr_failure_does_not_lose_post(s, db):
    p = post(db, 5, "Длинный нормальный текст про промпты для claude и нейросети", "photo")
    assert asyncio.run(enr(s, db, ocr=_boom).process(p)) == "enriched"
    assert db.get_post(p["id"])["attempts"] in (0, None)
