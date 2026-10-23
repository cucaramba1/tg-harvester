"""Чтение каналов: фаза «новое» по всем каналам, потом ретро канал за каналом, плюс живые события.

В Telegram ничего не отмечается прочитанным, прогресс живёт в SQLite."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .config import Channel, Settings
from .db import DB

log = logging.getLogger(__name__)
UTC = timezone.utc


def media_kind(m) -> str | None:
    if getattr(m, "photo", None):
        return "photo"
    if getattr(m, "voice", None):
        return "voice"
    if getattr(m, "video", None) or getattr(m, "video_note", None):
        return "video"
    if getattr(m, "audio", None):
        return "audio"
    return None


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), UTC)


class TgClient:
    """Тонкая обёртка над Telethon: всё, что нужно сборщику и обогатителю."""

    def __init__(self, client):
        self.c = client

    async def read_mark(self, chat_id: int) -> int:
        from telethon import functions, types
        peer = types.InputDialogPeer(peer=await self.c.get_input_entity(chat_id))
        res = await self.c(functions.messages.GetPeerDialogsRequest(peers=[peer]))
        return res.dialogs[0].read_inbox_max_id

    def iter_messages(self, chat_id: int, **kw):
        return self.c.iter_messages(chat_id, **kw)

    async def first_before(self, chat_id: int, when: datetime) -> int | None:
        ms = await self.c.get_messages(chat_id, limit=1, offset_date=when)
        return ms[0].id if ms else None

    async def messages_meta(self, chat_id: int, ids: list[int]) -> dict[int, tuple]:
        """document id, размер и длительность видео по id сообщений, без скачивания. Удалённых в ответе нет."""
        out = {}
        for m in await self.c.get_messages(chat_id, ids=ids):
            if m is not None and m.media and m.document is not None and m.file is not None:
                out[m.id] = (m.document.id, m.file.size, m.file.duration)
        return out

    async def download(self, chat_id: int, msg_id: int, dest: Path) -> Path | None:
        m = await self.c.get_messages(chat_id, ids=msg_id)
        if m is None or not m.media:
            return None
        p = await self.c.download_media(m, file=str(dest) + "/")
        return Path(p) if p else None


class Collector:
    def __init__(self, s: Settings, db: DB, tg, today=date.today):
        self.s, self.db, self.tg, self.today = s, db, tg, today

    async def init(self) -> None:
        floor_at = _midnight(self.s.retro_until)
        for ch in self.s.channels:
            if self.db.channel(ch.id) is not None:
                continue
            mark = await self.tg.read_mark(ch.id)
            floor = await self.tg.first_before(ch.id, floor_at) or 0
            mark = max(mark, floor)  # никогда не открывал канал: не ползём по годам истории
            self.db.save_channel(ch.id, read_mark=mark, new_cursor=mark, retro_cursor=mark + 1,
                                 retro_until=None, phase="new")
            log.info("канал %s: старт с id %s", ch.name, mark)

    def _next(self) -> tuple[Channel, object] | None:
        for phase in ("new", "retro"):
            for ch in self.s.channels:
                st = self.db.channel(ch.id)
                if st is not None and st["phase"] == phase:
                    return ch, st
        return None

    async def step(self) -> bool:
        nxt = self._next()
        if nxt is None:
            return False
        ch, st = nxt
        if st["phase"] == "new":
            await self._step_new(ch, st)
        else:
            await self._step_retro(ch, st)
        if self.s.request_pause:
            await asyncio.sleep(self.s.request_pause)
        return True

    async def _step_new(self, ch: Channel, st) -> None:
        cursor, n = st["new_cursor"], 0
        async for m in self.tg.iter_messages(ch.id, limit=self.s.chunk, min_id=cursor, reverse=True):
            self._store(ch.id, m, "history")
            cursor, n = max(cursor, m.id), n + 1
        fields: dict = {"new_cursor": cursor}
        if n < self.s.chunk:
            fields["phase"] = "retro" if ch.backfill else "done"
            log.info("канал %s: новое дочитано", ch.name)
        self.db.save_channel(ch.id, **fields)

    async def _step_retro(self, ch: Channel, st) -> None:
        until = date.fromisoformat(st["retro_until"]) if st["retro_until"] else await self._preflight(ch, st)
        until_at = _midnight(until)
        cursor, n, done = st["retro_cursor"], 0, False
        async for m in self.tg.iter_messages(ch.id, limit=self.s.chunk, max_id=cursor):
            n += 1
            if m.date < until_at:
                done = True
                break
            self._store(ch.id, m, "history")
            cursor = min(cursor, m.id)
        fields: dict = {"retro_cursor": cursor}
        if done or n < self.s.chunk:
            fields["phase"] = "done"
            log.info("канал %s: ретро закончено", ch.name)
        self.db.save_channel(ch.id, **fields)

    async def _preflight(self, ch: Channel, st) -> date:
        until = self.s.retro_until
        edge = await self.tg.first_before(ch.id, _midnight(until)) or 0
        count = st["retro_cursor"] - edge
        if count > self.s.retro_cap:
            until = self.today() - timedelta(days=self.s.retro_fallback_days)
            self.db.push_alert(f"✂️ {ch.name}: за год около {count} постов, ретро урезано до {until.isoformat()}")
        self.db.kv_set(f"preflight:{ch.id}", json.dumps({"count": count, "until": until.isoformat()}))
        self.db.save_channel(ch.id, retro_until=until.isoformat())
        return until

    def _store(self, chat_id: int, m, source: str) -> None:
        self.db.add_post(chat_id, m.id, m.grouped_id, m.date.isoformat(), m.message or "", media_kind(m), source)

    async def on_new(self, event) -> None:
        self._store(event.chat_id, event.message, "live")
