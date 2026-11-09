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

from .autovideo import calibration_picks
from .brain import alternatives
from .cards import (REASONS, REJECT_REASONS, reject_kb, alts_kb, audit_card, audit_kb, audit_text, confirm_kb, draft_card, draft_kb, eval_card, eval_kb, more_kb,
                    more_text, night_text, pilot_card, pilot_kb, reason_kb, status_text, topic_card, topic_kb,
                    topic_report_card, topic_report_kb, video_card, video_kb, written_kb)
from .config import Settings
from .db import DB
from .files import drop_keep, list_sections
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
        for step in (self.send_alerts, self.send_reports, self.send_topic_asks, self.send_topic_samples, self.send_topic_reports,
                     self.send_video_asks, self.send_cards, self.send_audits,
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

    def _video_batch(self, n: int) -> list:
        """Сначала калибровка (каналы, где мало ответов), остаток слотов лучшие по похожести, как раньше.
        При вопросах по темам по одному спрашиваем только посты мелких тем, посты без темы ждут разметки."""
        if n <= 0:
            return []  # почти всегда так: не гоняем тяжёлые выборки по очереди каждую минуту
        by_topic = self.s.video_by_topic
        pool = self.db.unmanaged_video_asks(10**6, self.s.topic_min_size) if by_topic else None
        allowed = {p["id"] for p in pool} if pool is not None else None
        picks = [p for p in calibration_picks(self.s, self.db, n) if allowed is None or p["id"] in allowed]
        if len(picks) < n:
            seen = {p["id"] for p in picks}
            rest = pool if pool is not None else self.db.video_asks(n + len(picks))
            picks += [p for p in rest if p["id"] not in seen][: n - len(picks)]
        return picks

    async def send_topic_asks(self, bot) -> None:
        """Одна карточка на тему: крупные темы первыми, не больше topic_cards_per_day в день."""
        if not self.s.video_by_topic or not self._day_open():
            return
        key = f"topic_cards:{self.now().date().isoformat()}"
        left = self.s.topic_cards_per_day - int(self.db.kv_get(key, "0"))
        for r in self.db.topic_overview():
            if left <= 0:
                break
            too_big = r["n"] > self.s.topic_max_size and self.db.kv_get(f"split_giveup:{r['topic']}") != "1"
            # state 'new' = карточку не смогли отправить (сеть, 429): пробуем снова, а не бросаем тему навсегда
            if r["n"] < self.s.topic_min_size or too_big or self.db.topic_state_of(r["topic"]) not in (None, "new"):
                continue
            tid = self.db.ensure_topic_state(r["topic"])
            samples = [p["text"] for p in self.db.topic_queue(r["topic"])[:3]]
            msg = await self._send(bot, topic_card(r["topic"], r["n"], r["files"], samples), topic_kb(tid))
            self.db.set_topic_state(tid, "asked", msg_id=msg.message_id)
            left -= 1
            self.db.kv_set(key, str(self.s.topic_cards_per_day - left))

    async def send_topic_samples(self, bot) -> None:
        """«Показать 3»: обычные видео-карточки по трём разным файлам темы."""
        for key, raw in self.db.kv_prefix("show3:"):
            for pid in json.loads(raw):
                p = self.db.get_post(pid)
                if p is None or p["video_state"] != "ask":
                    continue
                await self._send(bot, video_card(p, self.s.channel_name(p["chat_id"]), post_link(p["chat_id"], p["msg_id"])),
                                 video_kb(pid))
                self.db.update_post(pid, video_state="asked")
            self.db.kv_set(key, "")

    async def send_topic_reports(self, bot) -> None:
        """Итог проверки темы: список с кнопками, а в режиме topic_review=auto сразу черновик."""
        if not self._day_open():
            return
        for t in self.db.c.execute("SELECT * FROM topic_state WHERE state='checked' AND report IS NOT NULL").fetchall():
            rep = json.loads(t["report"])
            if self.s.topic_review == "auto":
                err = self._topic_to_draft(t["id"])
                note = "📝 Записал черновик, он придёт карточкой" if err is None else f"⚠️ {err}"
                await self._send(bot, f"{topic_report_card(rep)}\n\n{note}")
                continue
            msg = await self._send(bot, topic_report_card(rep), topic_report_kb(t["id"]))
            self.db.set_topic_state(t["id"], "reported", msg_id=msg.message_id)

    def _topic_to_draft(self, tid: int) -> str | None:
        """Одна общая заметка по теме обычным черновиком (дальше ✅/✏️ как всегда). Ошибка текстом, иначе None."""
        t = self.db.topic_by_id(tid)
        rep = json.loads(t["report"])
        if not rep["groups"]:
            return "в теме нечего записывать: у всех видео нет описания"
        sections = list_sections(self.s.vaults)
        first = self.db.get_post(rep["groups"][0]["posts"][0])
        target = first["target"] if first["mode"] == "new" and first["target"] in sections else self.s.topic_default_target
        if target not in sections:
            return f"раздела «{target}» нет в vault, поправь topic_default_target"
        lines = []
        for g in rep["groups"]:
            times = f" (в {g['count']} видео)" if g["count"] > 1 else ""
            lines.append(f"- **{g['title']}**{times}" + (f": {g['note']}" if g["note"] else ""))
        self.db.add_draft(first["id"], "new", target, t["topic"].capitalize(), "\n".join(lines), False,
                          alternatives(first, sections, self.s.vaults))
        posts = {p for g in rep["groups"] for p in g["posts"]} | {u["post_id"] for u in rep["undescribed"]}
        for pid in posts:
            if pid == first["id"]:
                self.db.update_post(pid, status="drafted", video_state="done")
            else:
                self.db.update_post(pid, status="rejected", reason=f"тема «{t['topic']}»: в общей заметке", video_state="done")
                drop_keep(self.s, pid)
        self.db.set_topic_state(tid, "written")
        return None

    def _distinct_files(self, topic: str, n: int) -> list[int]:
        seen, out = set(), []
        for p in self.db.topic_queue(topic):
            m = self.db.get_meta(p["id"])
            doc = m["doc_id"] if m and m["doc_id"] is not None else -p["id"]
            if doc not in seen:
                seen.add(doc)
                out.append(p["id"])
            if len(out) == n:
                break
        return out

    async def send_video_asks(self, bot) -> None:
        day = self.now().date().isoformat()
        key = f"video_asks:{day}"
        step = self.s.video_asks_per_day
        sent = int(self.db.kv_get(key, "0"))
        pending = int(self.db.kv_get("video_more", "0"))  # «Да» на «полистать ещё?»: не зависит ни от даты, ни от часа
        day_room = max(0, step - sent) if self._day_open() else 0
        for p in self._video_batch(day_room + pending):
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

    async def eval_batch(self, bot, n: int = 50) -> int:
        """Eval-набор: n случайных черновиков без видео, 👍/👎 пишут метку source=eval, в vault ничего не уходит.
        Отправленные запоминаются в kv, повтор команды не шлёт те же посты. Возвращает число отправленных."""
        seen = {int(x) for x in (self.db.kv_get("eval_sent", "") or "").split(",") if x}
        sample = self.db.eval_sample(n, seen)
        for i, d in enumerate(sample, 1):
            post = self.db.get_post(d["post_id"])
            link = post_link(post["chat_id"], post["msg_id"])
            await self._send(bot, eval_card(d, post, self.s.channel_name(post["chat_id"]), link, i, len(sample)), eval_kb(post["id"], link))
            seen.add(post["id"])
            self.db.kv_set("eval_sent", ",".join(map(str, sorted(seen))))
            await asyncio.sleep(self.pause)
        return len(sample)

    def status(self) -> str:
        phases = [(c.name, (self.db.channel(c.id) or {"phase": "ещё не начат"})["phase"]) for c in self.s.channels]
        return status_text(self.db.counts(), phases)

    # --- кнопки
    def handle_callback(self, data: str):
        kind, _, rest = data.partition(":")
        h = {"ok": self.on_ok, "no": self.on_no, "nr": self.on_nr, "alt": self.on_alt, "back": self.on_back, "to": self.on_to,
             "rb": self.on_rb, "rby": self.on_rby, "rbn": self.on_rbn, "vy": self.on_vy, "vn": self.on_vn, "vr": self.on_vr,
             "tc": self.on_tc, "t3": self.on_t3, "tx": self.on_tx,
             "tw": self.on_tw, "tn": self.on_tn,
             "pl": self.on_pl, "ev": self.on_ev, "mv": self.on_mv, "ab": self.on_ab, "ak": self.on_ak}.get(kind)
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
        if d["status"] not in ("drafted", "sent"):
            return None, None  # второй тап по ✅: не затираем «Записано» и кнопку отката
        text, kb = self._card(d)
        try:
            bid = self.writer.apply(did)
        except WriteError as e:
            return f"{text}\n\n⚠️ Не записал: {e}", kb  # кнопки остаются: можно сменить цель или отклонить
        self.db.stamp_decision(did)
        b = self.db.get_block(bid)
        return f"{text}\n\n✅ Записано в [[{PurePosixPath(b['path']).stem}]]", written_kb(bid)

    def on_no(self, did: int):
        d = self.db.get_draft(did)
        if d is None or d["status"] not in ("drafted", "sent"):
            return "Уже обработано", None
        text, _ = self._card(d)
        self.db.update_draft(did, status="rejected")
        self.db.stamp_decision(did)
        self.db.update_post(d["post_id"], status="rejected", reason="отклонил Некит")
        drop_keep(self.s, d["post_id"])
        return f"{text}\n\n❌ Отклонено", reject_kb(did)  # причина по желанию: пойдёт в метки для eval

    def on_nr(self, did: int, code: int):
        d = self.db.get_draft(did)
        if d is None or d["status"] != "rejected":
            return None, None  # старый или чужой тап: живой черновик не трогаем
        why = REJECT_REASONS.get(code)
        if why is None:  # 0 = «без причины»: просто убираем кнопки
            text, _ = self._card(d)
            return f"{text}\n\n❌ Отклонено", None
        self.db.update_post(d["post_id"], reason=f"отклонил Некит: {why}")
        text, _ = self._card(d)
        return f"{text}\n\n❌ Отклонено: {why}", None

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

    def on_tx(self, tid: int):
        t = self.db.topic_by_id(tid)
        if t is None or t["state"] in ("closed", "written"):
            return "Уже обработано", None
        posts = self.db.topic_queue(t["topic"])
        for p in posts:
            self.db.update_post(p["id"], status="rejected", reason="тема: не надо", video_state="no")
            drop_keep(self.s, p["id"])
        self.db.set_topic_state(tid, "closed")
        return f"🚫 Тема «{t['topic']}» закрыта, видео: {len(posts)}", None

    def on_tw(self, tid: int):
        t = self.db.topic_by_id(tid)
        if t is None or t["state"] != "reported":
            return "Уже обработано", None
        err = self._topic_to_draft(tid)
        return (f"⚠️ {err}", topic_report_kb(tid)) if err else ("📝 Черновик создан, придёт карточкой", None)

    def on_tn(self, tid: int):
        t = self.db.topic_by_id(tid)
        if t is None or t["state"] != "reported":
            return "Уже обработано", None
        rep = json.loads(t["report"])
        posts = {p for g in rep["groups"] for p in g["posts"]} | {u["post_id"] for u in rep["undescribed"]}
        for pid in posts:
            self.db.update_post(pid, status="rejected", reason="тема: итог не нужен", video_state="done")
            drop_keep(self.s, pid)
        self.db.set_topic_state(tid, "closed")
        return f"🗑 Тема «{t['topic']}» закрыта, видео: {len(posts)}", None

    def on_tc(self, tid: int):
        t = self.db.topic_by_id(tid)
        if t is None or t["state"] not in ("asked", "new"):
            return "Уже обработано", None
        self.db.set_topic_state(tid, "check")
        return f"🔬 Тема «{t['topic']}»: проверю ночью, утром пришлю итог", None

    def on_t3(self, tid: int):
        t = self.db.topic_by_id(tid)
        if t is None or t["state"] != "asked":
            return "Уже обработано", None
        self.db.kv_set(f"show3:{tid}", json.dumps(self._distinct_files(t["topic"], 3)))
        return None, topic_kb(tid, with_samples=False)

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

    def on_ev(self, pid: int, useful: int):
        self.db.add_label(pid, bool(useful), source="eval")
        return ("👍 писать" if useful else "👎 не надо") + ": учтено в проверке фильтра", None

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

    @r.message(owner, Command("eval"))
    async def _eval(m: Message):
        await m.answer("🧪 Шлю 50 случайных черновиков без видео. 👍 писать / 👎 не надо, в vault ничего не уходит")
        n = await app.eval_batch(m.bot)
        await m.answer(f"Отправлено {n}. Ответишь на все, скажи Claude: посчитаю порог по ним" if n else "Подходящих черновиков больше нет")

    @r.message(owner, Command("calibrate"))
    async def _calibrate(m: Message):
        res = calibrate(app.s, app.db)
        await m.answer(f"Новые пороги: {res}" if res else "Мало разметки: нужен хотя бы один 👍 и один 👎")

    dp = Dispatcher()
    dp.include_router(r)
    return dp
