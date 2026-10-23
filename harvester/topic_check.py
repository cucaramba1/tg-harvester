"""Ночная проверка темы: по одному разу каждый уникальный файл (громкость, Whisper, OCR кадров), потом склейка роутером."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .brain import BrainError
from .config import Settings
from .db import DB
from .enricher import extract_video, free_gb, ocr_image
from .stt import transcribe
from .topics import clip, video_msg_id

log = logging.getLogger(__name__)

WINDOW = (1, 7)  # час начала и конца окна проверки
MERGE_SYSTEM = ("Тебе дают описания нескольких коротких видео на одну тему (подпись, речь, текст на экране). "
                "Разные подписи могут описывать одно и то же упражнение или совет. Объедини одинаковые по смыслу в группы, "
                "для каждой дай короткое название и одну фразу сути. Каждый post_id в одной группе. Ничего не выдумывай.")
MERGE_SCHEMA = {"groups": [{"title": "строка", "note": "строка", "post_ids": [0]}]}


def mean_volume(path: Path) -> float | None:
    """Средняя громкость дорожки в dB, None если звука нет совсем."""
    has = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    if not has:
        return None
    err = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(m.group(1)) if m else None


def foreign_whisper() -> bool:
    """Чужой mlx_whisper уже грузит память: свою расшифровку отложим. Вызывается, пока наша не запущена."""
    return subprocess.run(["pgrep", "-f", "mlx_whisper"], capture_output=True).returncode == 0


def _file_groups(db: DB, topic: str) -> list[list]:
    """Посты темы, сгруппированные по файлу: [представитель, повторы...]. Без метаданных пост сам по себе."""
    groups: dict[object, list] = {}
    for p in db.topic_queue(topic):
        m = db.get_meta(p["id"])
        key = m["doc_id"] if m is not None and m["doc_id"] is not None else ("own", p["id"])
        groups.setdefault(key, []).append(p)
    return list(groups.values())


async def _check_file(s: Settings, db: DB, tg, rep, *, extract, stt, ocr, loudness) -> None:
    speech = ocr_text = ""
    try:
        s.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=s.tmp_dir) as d:
            path = await tg.download(rep["chat_id"], video_msg_id(rep) or rep["msg_id"], Path(d))
            if path is not None:
                level = await asyncio.to_thread(loudness, path)
                audio, frames = await extract(path, Path(d))
                if audio and level is not None and level > s.topic_silent_db:
                    speech = (await stt(audio)).strip()
                ocr_text = " | ".join(t for f in frames if (t := (await asyncio.to_thread(ocr, f)).strip()))
    except Exception as e:
        log.exception("проверка файла поста %s", rep["id"])
        db.push_alert(f"🎬 Не смог проверить видео поста {rep['id']}: {e}"[:300])
    db.c.execute("INSERT OR REPLACE INTO topic_checks(post_id, silent, speech, ocr) VALUES(?, ?, ?, ?)",
                 (rep["id"], int(not speech and not ocr_text), speech, ocr_text))


def _merge_prompt(items: list[dict]) -> str:
    return "Видео по теме:\n\n" + "\n\n".join(
        f"post_id {i['post_id']}\nподпись: {i['caption']}\nречь: {i['speech'] or 'нет'}\nтекст на экране: {i['ocr'] or 'нет'}"
        for i in items)


async def _merge(s: Settings, db: DB, router, topic: str, groups: list[list]) -> dict | None:
    """Сводка темы. None, если роутер не ответил: тема остаётся в check, файлы повторно не качаем."""
    files = []
    for g in groups:
        rep = g[0]
        c = db.c.execute("SELECT * FROM topic_checks WHERE post_id=?", (rep["id"],)).fetchone()
        files.append((g, c))
    described = [(g, c) for g, c in files if c is not None and not c["silent"]]
    undescribed = [{"post_id": g[0]["id"], "caption": clip(g[0]["text"], 120)} for g, c in files if c is None or c["silent"]]
    by_rep = {g[0]["id"]: [p["id"] for p in g] for g, _ in described}
    out_groups: list[dict] = []
    if described:
        items = [{"post_id": g[0]["id"], "caption": clip(g[0]["text"], 200), "speech": clip(c["speech"], 500),
                  "ocr": clip(c["ocr"], 300)} for g, c in described]
        try:
            raw = await router(s, _merge_prompt(items), system=MERGE_SYSTEM, schema=MERGE_SCHEMA, timeout=s.claude_timeout)
        except BrainError as e:
            log.warning("склейка темы %s: роутер недоступен: %s", topic, e)
            return None
        seen: set[int] = set()
        for it in (raw.get("groups") or []) if isinstance(raw, dict) else []:
            reps = [r for r in dict.fromkeys(it.get("post_ids") or []) if r in by_rep and r not in seen]
            if not reps:
                continue
            seen.update(reps)
            posts = [pid for r in reps for pid in by_rep[r]]
            out_groups.append({"title": str(it.get("title") or "").strip()[:80] or "без названия",
                               "note": str(it.get("note") or "").strip()[:240], "posts": posts, "count": len(posts)})
        for (g, c) in described:  # роутер кого-то не назвал: такой ролик сам себе группа
            if g[0]["id"] not in seen:
                out_groups.append({"title": clip(g[0]["text"].split("\n")[0], 80) or "без названия", "note": "",
                                   "posts": [p["id"] for p in g], "count": len(g)})
    return {"topic": topic, "total": sum(len(g) for g in groups), "files": len(groups),
            "groups": out_groups, "undescribed": undescribed}


async def run_topic_checks(s: Settings, db: DB, tg, router, *, now=datetime.now, extract=extract_video, stt=None,
                           ocr=ocr_image, loudness=mean_volume, foreign_whisper=foreign_whisper, disk=free_gb,
                           force: bool = False) -> dict:
    """Один проход: проверить непроверенные файлы тем в состоянии check (в пределах бюджета ночи) и склеить готовые темы."""
    stt = stt or (lambda p: transcribe(s, p))
    stamp = now()
    rep = {"checked": 0, "merged": 0}
    if not force and not (WINDOW[0] <= stamp.hour < WINDOW[1]):
        return {**rep, "skipped": "вне окна"}
    key = f"topic_check:{stamp.date().isoformat()}"
    used = int(db.kv_get(key, "0"))
    for t in db.c.execute("SELECT * FROM topic_state WHERE state='check' ORDER BY id").fetchall():
        groups = _file_groups(db, t["topic"])
        for g in groups:
            if db.c.execute("SELECT 1 FROM topic_checks WHERE post_id=?", (g[0]["id"],)).fetchone():
                continue
            if used >= s.topic_check_per_night:
                return {**rep, "skipped": "бюджет ночи"}
            if disk() < s.min_free_gb:
                db.push_alert(f"💾 На диске меньше {s.min_free_gb:g} GB: проверку тем остановил")
                return {**rep, "skipped": "диск"}
            if foreign_whisper():
                return {**rep, "skipped": "чужой Whisper"}
            await _check_file(s, db, tg, g[0], extract=extract, stt=stt, ocr=ocr, loudness=loudness)
            used += 1
            rep["checked"] += 1
            db.kv_set(key, str(used))
        report = await _merge(s, db, router, t["topic"], groups)
        if report is not None:
            db.set_topic_state(t["id"], "checked", report=json.dumps(report, ensure_ascii=False))
            rep["merged"] += 1
    return rep
