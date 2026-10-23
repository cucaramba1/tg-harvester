"""Бот ревью: карточки черновиков, видео-вопросы, пилот, откат, отчёты. Пишет только владельцу."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import PurePosixPath

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from .cards import (REASONS, alts_kb, audit_card, audit_kb, audit_text, confirm_kb, draft_card, draft_kb, more_kb,
                    more_text, night_text, pilot_card, pilot_kb, reason_kb, status_text, video_card, video_kb,
                    written_kb)
from .config import Settings
from .db import DB
from .files import drop_keep
from .matcher import calibrate
from .writer import WriteError, Writer, post_link

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class App:
    def __init__(self, s: Settings, db: DB, writer: Writer, now=datetime.now, pause: float = 1.1):
        self.s, self.db, self.writer, self.now, self.pause = s, db, writer, now, pause

    # --- исходящие
    async def _send(self, bot, text: str, kb=None, *, prompt: bool = False):
        msg = await bot.send_message(self.s.owner_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        if not prompt and self.db.kv_get("more_msg"):
            self.db.kv_set("more_stale", "1")  # после вопроса «ещё?» ушло другое сообщение: перенесём вопрос вниз
        return msg

    def _day_open(self) -> bool:
        return self.now().hour >= self.s.cards_hour

    async def tick(self, bot) -> None:
        for step in (self.send_alerts, self.send_reports, self.send_video_asks, self.send_cards, self.send_audits,
                     self.send_more_prompt):
            try:
                await step(bot)
            except Exception:
                log.exception("tick %s", step.__name__)

    async def send_alerts(self, bot) -> None:
        for t in self.db.pop_alerts():
            await self._send(bot, t)

    async def send_reports(self, bot) -> None:
        if not self._day_open():
            return
        for key, render in (("night_report", night_text), ("audit_report", audit_text)):
            raw = self.db.kv_get(key)
            if raw and self.db.kv_get(key + "_sent") != raw:
                await self._send(bot, render(json.loads(raw)))
                self.db.kv_set(key + "_sent", raw)

    async def send_video_asks(self, bot) -> None:
        day = self.now().date().isoformat()
        key = f"video_asks:{day}"
        step = self.s.video_asks_per_day
        sent = int(self.db.kv_get(key, "0"))
        pending = int(self.db.kv_get("video_more", "0"))  # «Да» на «полистать ещё?»: не зависит ни от даты, ни от часа
        day_room = max(0, step - sent) if self._day_open() else 0
        for p in self.db.video_asks(day_room + pending):
            await self._send(bot, video_card(p, self.s.channel_name(p["chat_id"]), post_link(p["chat_id"], p["msg_id"])),
                             video_kb(p["id"]))
            self.db.update_post(p["id"], video_state="asked")
            if day_room > 0:
                day_room -= 1
                sent += 1
                self.db.kv_set(key, str(sent))
            else:
                pending -= 1
                self.db.kv_set("video_more", str(pending))

    async def _drop_prompt(self, bot) -> None:
        old = self.db.kv_get("more_msg")
        if old:
            try:
                await bot.delete_message(self.s.owner_id, int(old))
            except Exception:
                log.warning("не удалил старый вопрос «ещё?» %s", old)
        for k in ("more_msg", "more_day", "more_stale"):
            self.db.kv_set(k, "")

    async def send_more_prompt(self, bot) -> None:
        """«Полистать ещё?» всегда последнее сообщение: последний шаг тика, а если после него что-то ушло, переотправляем.
        Появляется, только когда на все выданные видео отвечено; в полночь без ответа удаляется."""
        day = self.now().date().isoformat()
        step = self.s.video_asks_per_day
        left = self.db.video_ask_count()
        old = self.db.kv_get("more_msg")
        if old:
            if self.db.kv_get("more_day") != day or not left:
                return await self._drop_prompt(bot)
            if self.db.kv_get("more_stale") != "1":
                return
            await self._drop_prompt(bot)
            ask = True
        else:
            ask = False
        sent = int(self.db.kv_get(f"video_asks:{day}", "0"))
        pending = int(self.db.kv_get("video_more", "0"))
        token = f"{day}:{self.db.kv_get('video_yes', '0')}"  # на каждом «уровне» спрашиваем один раз
        if not ask:
            ask = (self._day_open() and sent >= step and pending == 0 and left and not self.db.video_asked_count()
                   and self.db.kv_get("video_prompt") != token)
        if not ask:
            return
        self.db.kv_set("video_prompt", token)
        msg = await self._send(bot, more_text(sent, left, step), more_kb(), prompt=True)
        self.db.kv_set("more_msg", str(msg.message_id))
        self.db.kv_set("more_day", day)
        self.db.kv_set("more_stale", "")

    def _card(self, d):
        post = self.db.get_post(d["post_id"])
        link = post_link(post["chat_id"], post["msg_id"])
        return draft_card(d, post, self.s.channel_name(post["chat_id"]), link), draft_kb(d["id"], link)

    async def send_cards(self, bot) -> None:
        if not self._day_open():
            return
        key = f"cards:{self.now().date().isoformat()}"
        sent = int(self.db.kv_get(key, "0"))
        for d in self.db.drafts("drafted", max(0, self.s.cards_per_day - sent)):
            text, kb = self._card(d)
            msg = await self._send(bot, text, kb)
            self.db.update_draft(d["id"], status="sent", card_msg_id=msg.message_id,
                                 sent_on=self.now().date().isoformat())
            sent += 1
            self.db.kv_set(key, str(sent))

    async def send_audits(self, bot) -> None:
        if not self._day_open():
            return
        for a in self.db.audits("new"):
            b = self.db.get_block(a["block_id"])
            if b is None or b["rolled_back"]:
                self.db.update_audit(a["id"], status="kept")
                continue
            await self._send(bot, audit_card(a, b), audit_kb(a["id"]))
            self.db.update_audit(a["id"], status="sent")

    async def pilot(self, bot, n: int = 50) -> None:
        for p in self.db.pilot_sample(n):
            await self._send(bot, pilot_card(p, self.s.channel_name(p["chat_id"]), post_link(p["chat_id"], p["msg_id"])), pilot_kb(p["id"]))
            await asyncio.sleep(self.pause)

    def status(self) -> str:
        phases = [(c.name, (self.db.channel(c.id) or {"phase": "ещё не начат"})["phase"]) for c in self.s.channels]
        return status_text(self.db.counts(), phases)

    # --- кнопки
    def handle_callback(self, data: str):
        kind, _, rest = data.partition(":")
        h = {"ok": self.on_ok, "no": self.on_no, "alt": self.on_alt, "back": self.on_back, "to": self.on_to,
             "rb": self.on_rb, "rby": self.on_rby, "rbn": self.on_rbn, "vy": self.on_vy, "vn": self.on_vn, "vr": self.on_vr,
             "pl": self.on_pl, "mv": self.on_mv, "ab": self.on_ab, "ak": self.on_ak}.get(kind)
        if h is None or not rest:
            return None, None
        try:
            return h(*map(int, rest.split(":")))
        except (ValueError, TypeError, IndexError):
            return None, None

    def on_ok(self, did: int):
        d = self.db.get_draft(did)
        if d is None:
            return "Черновик не найден", None
        text, _ = self._card(d)
        try:
            bid = self.writer.apply(did)
        except WriteError as e:
            return f"{text}\n\n⚠️ Не записал: {e}", None
        b = self.db.get_block(bid)
        return f"{text}\n\n✅ Записано в [[{PurePosixPath(b['path']).stem}]]", written_kb(bid)

    def on_no(self, did: int):
        d = self.db.get_draft(did)
        if d is None or d["status"] not in ("drafted", "sent"):
            return "Уже обработано", None
        text, _ = self._card(d)
        self.db.update_draft(did, status="rejected")
        self.db.update_post(d["post_id"], status="rejected", reason="отклонил Некит")
        drop_keep(self.s, d["post_id"])
        return f"{text}\n\n❌ Отклонено", None

    def on_alt(self, did: int):
        d = self.db.get_draft(did)
        return (None, alts_kb(did, json.loads(d["alts"]))) if d else ("Черновик не найден", None)

    def on_back(self, did: int):
        d = self.db.get_draft(did)
        return (None, self._card(d)[1]) if d else ("Черновик не найден", None)

    def on_to(self, did: int, i: int):
        d = self.db.get_draft(did)
        alts = json.loads(d["alts"]) if d else []
        if not 0 <= i < len(alts):
            return "Вариант устарел", None
        mode, target = alts[i]
        fields = {"mode": mode, "target": target}
        if mode == "new" and not d["title"]:
            fields["title"] = d["content"].strip().splitlines()[0][:80]
        self.db.update_draft(did, **fields)
        return self._card(self.db.get_draft(did))

    def on_rb(self, bid: int):
        return None, confirm_kb(bid)

    def on_rbn(self, bid: int):
        return None, written_kb(bid)

    def _rollback_text(self, res: str) -> str:
        return {"ok": "↩️ Откатил", "already": "Уже откачено", "gone": "⚠️ Файла уже нет",
                "changed": f"⚠️ Блок правили руками, откати вручную. Снапшоты: {self.s.snapshots_dir}"}[res]

    def on_rby(self, bid: int):
        return self._rollback_text(self.writer.rollback(bid)), None

    def on_vy(self, pid: int):
        self.db.update_post(pid, video_state="yes")
        self.db.add_label(pid, True, source="video")
        return "🎬 Разберу видео, карточка придёт после ночного разбора", None

    def on_vn(self, pid: int):
        self.db.update_post(pid, status="rejected", reason="видео: не надо", video_state="no")
        drop_keep(self.s, pid)
        self.db.add_label(pid, False, source="video")
        return "❌ Пропускаю. Причина (необязательно):", reason_kb(pid)

    def on_vr(self, pid: int, code: int):
        reason = REASONS.get(code)
        if reason is None:
            return None, None
        if self.db.c.execute("SELECT 1 FROM labels WHERE post_id=?", (pid,)).fetchone() is None:
            return "Не найдено", None
        self.db.set_label_reason(pid, reason)
        return f"❌ Пропускаю ({reason})", None

    def on_mv(self, yes: int):
        self.db.kv_set("more_msg", "")
        self.db.kv_set("more_stale", "")
        if not yes:
            return "Ок, на сегодня хватит", None
        step = self.s.video_asks_per_day
        self.db.kv_set("video_more", str(int(self.db.kv_get("video_more", "0")) + step))
        self.db.kv_set("video_yes", str(int(self.db.kv_get("video_yes", "0")) + 1))
        return f"Ок, ещё {step} придёт в ближайшие минуты", None

    def on_pl(self, pid: int, useful: int):
        self.db.add_label(pid, bool(useful))
        return ("👍" if useful else "👎") + " учтено", None

    def on_ab(self, aid: int):
        a = self.db.get_audit(aid)
        if a is None:
            return "Не найдено", None
        res = self.writer.rollback(a["block_id"])
        if res in ("ok", "already"):
            self.db.update_audit(aid, status="rolled_back")
        return self._rollback_text(res), None

    def on_ak(self, aid: int):
        self.db.update_audit(aid, status="kept")
        return "Оставил", None


def build_dispatcher(app: App) -> Dispatcher:
    r = Router()
    owner = F.from_user.id == app.s.owner_id

    @r.callback_query(owner)
    async def _cb(cq: CallbackQuery):
        text, kb = app.handle_callback(cq.data or "")
        if text is not None:
            await cq.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        elif kb is not None:
            await cq.message.edit_reply_markup(reply_markup=kb)
        await cq.answer()

    @r.message(owner, Command("status"))
    async def _status(m: Message):
        await m.answer(app.status())

    @r.message(owner, Command("pilot"))
    async def _pilot(m: Message):
        await m.answer("🧪 Шлю выборку постов, отметь каждый: полезно или мусор")
        await app.pilot(m.bot)

    @r.message(owner, Command("calibrate"))
    async def _calibrate(m: Message):
        res = calibrate(app.s, app.db)
        await m.answer(f"Новые пороги: {res}" if res else "Мало разметки: нужен хотя бы один 👍 и один 👎")

    dp = Dispatcher()
    dp.include_router(r)
    return dp
