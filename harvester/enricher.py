"""Обогащение без токенов: правила, OCR картинок (macOS Vision), whisper для аудио, видео только по запросу."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from datetime import date
from pathlib import Path

from .config import Settings
from .db import DB
from .files import drop_keep
from .rules import ad_reason
from .stt import transcribe

log = logging.getLogger(__name__)


VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
DISKCAP_FLAG = Path.home() / "Claude Code/tools/resource-guard/state/diskcap-tg-harvester"


def dir_gb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e9 if path.exists() else 0.0


def free_gb(path: Path = Path.home()) -> float:
    return shutil.disk_usage(path).free / 2**30


def ocr_image(path: Path) -> str:
    from ocrmac import ocrmac
    res = ocrmac.OCR(str(path), language_preference=["ru-RU", "en-US"]).recognize()
    return "\n".join(text for text, conf, _box in res if conf >= 0.3).strip()


async def _ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-loglevel", "error", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 600)
    finally:
        if proc.returncode is None:  # таймаут или отмена: ffmpeg-сирота жрал CPU и писал в удалённый tmp
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[-300:]}")


async def extract_video(video: Path, out: Path) -> tuple[Path | None, list[Path]]:
    audio = out / "audio.wav"
    try:
        await _ffmpeg("-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio))
    except RuntimeError:
        audio = None  # видео без звуковой дорожки
    await _ffmpeg("-i", str(video), "-vf", "fps=1/10", "-frames:v", "3", str(out / "frame_%d.jpg"))
    return (audio if audio and audio.exists() and audio.stat().st_size > 1000 else None), sorted(out.glob("frame_*.jpg"))


class Enricher:
    def __init__(self, s: Settings, db: DB, tg, *, ocr=ocr_image, stt=None, disk=free_gb, extract=extract_video):
        self.s, self.db, self.tg = s, db, tg
        self.ocr, self.disk, self.extract = ocr, disk, extract
        self.stt = stt or (lambda p: transcribe(s, p))

    async def process(self, post) -> str:
        try:
            return await self._process(post)
        except Exception as e:
            log.exception("enrich post %s", post["id"])
            drop_keep(self.s, post["id"])
            if self.db.bump_attempts(post["id"]) >= 3:
                self.db.update_post(post["id"], status="error", reason=f"enrich: {e}"[:300])
                return "error"
            return "new"

    def _disk_ok(self) -> bool:
        if DISKCAP_FLAG.exists():  # сторож: data/ бота выше DISK_CAPS (tools/resource-guard)
            return self._alert_once("diskcap_alert", "🗂 Данные бота выше лимита сторожа: медиа пропускаю, текст обрабатываю")
        if dir_gb(self.s.keep_dir) >= self.s.max_keep_gb:
            return self._alert_once("keep_alert", f"🗂 Папка медиа дошла до {self.s.max_keep_gb:g} GB: медиа пропускаю, текст обрабатываю")
        if self.disk() >= self.s.min_free_gb:
            return True
        return self._alert_once("disk_alert", f"💾 На диске меньше {self.s.min_free_gb:g} GB: медиа пропускаю, текст обрабатываю")

    def _alert_once(self, key: str, text: str) -> bool:
        today = date.today().isoformat()
        if self.db.kv_get(key) != today:
            self.db.kv_set(key, today)
            self.db.push_alert(text)
        return False

    async def _process(self, post) -> str:
        pid, text = post["id"], post["text"] or ""
        if reason := ad_reason(text, self.s.ad_markers):
            self.db.update_post(pid, status="filtered", reason=reason)
            return "filtered"
        media = json.loads(post["media"])
        photos = [m for m in media if m["kind"] == "photo"][: self.s.max_images]
        sounds = [m for m in media if m["kind"] in ("voice", "audio")]
        extra: list[str] = []
        images: list[str] = []
        if (photos or sounds) and self._disk_ok():
            keep = self.s.keep_dir / str(pid)
            for m in photos:
                keep.mkdir(parents=True, exist_ok=True)
                p = await self.tg.download(post["chat_id"], m["msg_id"], keep)
                if p is None:
                    continue
                if p.suffix.lower() in VIDEO_SUFFIXES:  # тип «photo», а файл видео (превью ссылки): не картинка
                    p.unlink(missing_ok=True)
                    continue
                images.append(str(p))
                try:
                    if t := await asyncio.to_thread(self.ocr, p):
                        extra.append(t)
                except Exception as e:  # битая картинка не должна терять пост с нормальным текстом
                    log.warning("ocr post %s: %s", pid, e)
            for m in sounds:
                extra += await self._transcribe(post["chat_id"], m["msg_id"])
        full = "\n".join([text, *extra]).strip()
        if len(full) < self.s.min_chars:
            self.db.update_post(pid, status="filtered", reason="short")
            drop_keep(self.s, pid)
            return "filtered"
        self.db.update_post(pid, status="enriched", extra_text="\n\n".join(extra), images=images)
        return "enriched"

    async def _transcribe(self, chat_id: int, msg_id: int) -> list[str]:
        self.s.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.s.tmp_dir) as d:
            p = await self.tg.download(chat_id, msg_id, Path(d))
            if p is None:
                return []
            t = await self.stt(p)
            return [t] if t else []

    async def analyze_video(self, post) -> None:
        """По кнопке «Разобрать»: звук -> whisper, 3 кадра -> OCR. При сбое идём дальше по подписи."""
        extra: list[str] = []
        try:
            if self._disk_ok():
                vids = [m for m in json.loads(post["media"]) if m["kind"] == "video"][:1]
                self.s.tmp_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=self.s.tmp_dir) as d:
                    for m in vids:
                        p = await self.tg.download(post["chat_id"], m["msg_id"], Path(d))
                        if p is None:
                            continue
                        audio, frames = await self.extract(p, Path(d))
                        if audio and (t := await self.stt(audio)):
                            extra.append(t)
                        for f in frames:
                            if t := await asyncio.to_thread(self.ocr, f):
                                extra.append(t)
        except Exception as e:
            log.exception("video post %s", post["id"])
            self.db.push_alert(f"🎬 Не смог разобрать видео поста {post['id']}: {e}"[:300])
        merged = "\n\n".join(x for x in [post["extra_text"], *extra] if x)
        self.db.update_post(post["id"], extra_text=merged, video_state="done", status="enriched")
