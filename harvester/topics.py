"""Темы видео-очереди: метаданные файла (повторы), разметка тем через роутер."""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict

from .brain import BrainError
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

TOPIC_SYSTEM = ("Ты раскладываешь короткие подписи к видео по темам. Тема это то, о чём видео: часть тела, проблема или "
                "тип совета (например «шея и осанка», «колени и суставы», «давление и сосуды», «питание», «сон», "
                "«мозг и память»). Одинаковые по смыслу посты получают ОДИНАКОВОЕ название, названия из 1-3 слов, "
                "строчными. Если подходит тема из списка уже существующих, используй её дословно.")
TOPIC_SCHEMA = {"items": [{"post_id": 0, "topic": "строка"}]}


def video_msg_id(post) -> int | None:
    """id сообщения с видео: у альбомов оно может отличаться от id поста."""
    for m in json.loads(post["media"]):
        if m.get("kind") == "video":
            return m["msg_id"]
    return None


async def fetch_meta(tg, db: DB, limit: int = 100) -> int:
    """document id, размер, длительность для вопросов очереди. Исчезнувшим ставим пустую запись, чтобы не спрашивать снова."""
    by_chat: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for p in db.queue_without_meta(limit):
        mid = video_msg_id(p) or p["msg_id"]
        by_chat[p["chat_id"]].append((p["id"], mid))
    done = 0
    for chat_id, pairs in by_chat.items():
        got = await tg.messages_meta(chat_id, [mid for _, mid in pairs])
        for pid, mid in pairs:
            doc, size, dur = got.get(mid, (None, None, None))
            db.save_meta(pid, doc, size, dur)
            done += 1
    return done


def clip(text: str, n: int = 260) -> str:
    return " ".join((text or "").replace("\n", " ").split())[:n]


def norm_topic(raw) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip().lower()[:40]


async def tag_topics(s: Settings, db: DB, router, limit: int = 80) -> int:
    """Тема для постов очереди одним запросом к роутеру. Сбой роутера ничего не меняет, повторим позже."""
    posts = db.queue_without_topic(limit)
    if not posts:
        return 0
    known = db.topic_names()
    prompt = ("Существующие темы: " + (", ".join(known) if known else "пока нет") + "\n\nРазложи посты по темам.\n\n"
              + "\n".join(f"{p['id']}: {clip(p['text'])}" for p in posts))
    try:
        raw = await router(s, prompt, system=TOPIC_SYSTEM, schema=TOPIC_SCHEMA, timeout=s.claude_timeout)
    except BrainError as e:
        log.warning("темы: роутер недоступен: %s", e)
        return 0
    valid = {p["id"] for p in posts}
    n = 0
    for it in (raw.get("items") or []) if isinstance(raw, dict) else []:
        try:
            pid, topic = int(it["post_id"]), norm_topic(it["topic"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid in valid and topic:
            db.set_topic(pid, topic)
            n += 1
    return n


SPLIT_SYSTEM = ("Тебе дают посты на одну широкую тему. Раздели их на узкие подтемы: конкретная часть тела, проблема, "
                "инструмент или жанр, примерно по 5-40 постов в каждой. Одинаковые по смыслу посты получают ОДИНАКОВОЕ "
                "название подтемы (1-3 слова, строчными, без названия родительской темы). Если подходит подтема из "
                "списка существующих, используй её дословно.")


async def split_big_topics(s: Settings, db: DB, router, limit: int = 80) -> int:
    """Одна слишком широкая тема за вызов: порция её постов раскладывается по подтемам «тема: подтема».
    Три пустых ответа подряд и тема остаётся как есть (split_giveup), карточка по ней всё равно идёт."""
    for r in db.topic_overview():
        topic = r["topic"]
        if r["n"] <= s.topic_max_size or db.kv_get(f"split_giveup:{topic}") == "1":
            continue
        posts = db.topic_queue(topic)[:limit]
        known = [t.split(": ", 1)[1] for t in db.topic_names(300) if t.startswith(topic + ": ")]
        prompt = (f"Широкая тема: {topic}\nСуществующие подтемы: " + (", ".join(known) if known else "пока нет")
                  + "\n\nРазложи посты по подтемам.\n\n" + "\n".join(f"{p['id']}: {clip(p['text'])}" for p in posts))
        try:
            raw = await router(s, prompt, system=SPLIT_SYSTEM, schema=TOPIC_SCHEMA, timeout=s.claude_timeout)
        except BrainError as e:
            log.warning("подтемы %s: роутер недоступен: %s", topic, e)
            return 0
        valid = {p["id"] for p in posts}
        n = 0
        for it in (raw.get("items") or []) if isinstance(raw, dict) else []:
            try:
                pid, sub = int(it["post_id"]), norm_topic(str(it["topic"]).split(": ", 1)[-1])
            except (KeyError, TypeError, ValueError):
                continue
            if pid in valid and sub and sub != topic:
                db.set_topic(pid, f"{topic}: {sub}")
                n += 1
        fails = 0 if n else int(db.kv_get(f"split_fail:{topic}", "0")) + 1
        db.kv_set(f"split_fail:{topic}", str(fails))
        if fails >= 3:
            db.kv_set(f"split_giveup:{topic}", "1")
        return n
    return 0
