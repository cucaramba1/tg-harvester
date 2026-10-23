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
