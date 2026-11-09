"""Пакеты записи в vault: сервер просит, ноут пишет настоящим Writer, ответ (ack) возвращается на сервер.

Сервер (apply_mode=packet): PacketWriter кладёт запрос в data/outbox, read_acks применяет ответы ноута к базе сервера.
Ноут: apply_packet прогоняет пакет через обычный Writer и его журнал блоков (откат работает как раньше).
Повторная доставка безопасна: записанный черновик второй раз не пишется, ack строится из уже существующего блока."""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from .config import Settings
from .db import DB
from .files import drop_keep
from .writer import WriteError, Writer

log = logging.getLogger(__name__)


def _atomic_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)  # ноут не должен увидеть половину файла


def make_writer(s: Settings, db: DB):
    return PacketWriter(s, db) if s.apply_mode == "packet" else Writer(s, db)


class PacketWriter:
    """Вместо записи в vault кладёт пакет. apply() возвращает None (запись будет позже), rollback() возвращает 'queued'."""

    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db

    def apply(self, did: int) -> None:
        d = self.db.get_draft(did)
        if d is None or d["status"] not in ("drafted", "sent"):
            raise WriteError("черновик уже обработан")
        post = self.db.get_post(d["post_id"])
        key = f"apply-{did}"
        out = self.s.outbox_dir
        out.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        if d["keep_image"]:
            for i, p in enumerate(map(Path, json.loads(post["images"]))):
                if p.exists():
                    dest = out / key / f"img{i}{p.suffix}"
                    dest.parent.mkdir(exist_ok=True)
                    shutil.copy2(p, dest)
                    names.append(dest.name)
        _atomic_json(out / f"{key}.json", {"key": key, "kind": "apply", "draft": dict(d),
                                           "post": {**dict(post), "images": names}})  # json пишется последним
        self.db.update_draft(did, status="queued")

    def rollback(self, bid: int) -> str:
        b = self.db.get_block(bid)
        if b is None or b["rolled_back"]:
            return "already"
        self.s.outbox_dir.mkdir(parents=True, exist_ok=True)
        key = f"rollback-{bid}"
        _atomic_json(self.s.outbox_dir / f"{key}.json", {"key": key, "kind": "rollback", "block_id": bid})
        return "queued"


# --- ноут: применить пакет ---------------------------------------------------------------------------------------

def apply_packet(s: Settings, db: DB, packet: dict, files_dir: Path) -> dict:
    """Пакет -> ack. Ошибки записи не бросает, а возвращает в ack (ok=False), чтобы сервер показал их Некиту."""
    key, kind = packet["key"], packet["kind"]
    if kind == "rollback":
        bid = int(packet["block_id"])
        if db.get_block(bid) is None:
            return {"key": key, "kind": kind, "ok": False, "block_id": bid, "error": f"блока {bid} нет в базе ноута"}
        return {"key": key, "kind": kind, "ok": True, "block_id": bid, "res": Writer(s, db).rollback(bid)}
    if kind != "apply":
        return {"key": key, "kind": kind, "ok": False, "error": f"неизвестный тип пакета: {kind}"}
    d, post = packet["draft"], packet["post"]
    did = int(d["id"])
    have = db.get_draft(did)
    if have is not None and have["status"] == "written" and (b := db.block_for_draft(did)) is not None:
        return {"key": key, "kind": kind, "ok": True, "block": dict(b)}  # повторная доставка: уже записано
    images = [str(files_dir / key / n) for n in post["images"]]
    db.upsert_row("posts", {**post, "images": json.dumps(images)})
    db.upsert_row("drafts", {**d, "status": "drafted"})
    try:
        bid = Writer(s, db).apply(did)
    except WriteError as e:
        return {"key": key, "kind": kind, "ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 диск, права: сервер покажет текст, пакет можно отправить заново
        log.exception("apply %s", key)
        return {"key": key, "kind": kind, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"key": key, "kind": kind, "ok": True, "block": dict(db.get_block(bid))}


def apply_inbox(s: Settings, db: DB) -> list[str]:
    """Все пакеты из data/inbox -> acks_out/<key>.json. Возвращает ключи обработанных."""
    s.acks_out_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for f in sorted(s.inbox_dir.glob("*.json")) if s.inbox_dir.exists() else []:
        try:
            packet = json.loads(f.read_text("utf-8"))
        except ValueError:
            log.error("битый пакет %s, пропускаю", f.name)
            continue
        ack = apply_packet(s, db, packet, s.inbox_dir)
        _atomic_json(s.acks_out_dir / f"{packet['key']}.json", ack)
        done.append(packet["key"])
    return done


# --- сервер: применить ответы ноута -------------------------------------------------------------------------------

def read_acks(s: Settings, db: DB) -> list[dict]:
    """Ответы ноута -> база сервера. Возвращает события для уведомлений в боте."""
    events: list[dict] = []
    if not s.acks_dir.exists():
        return events
    done_dir = s.acks_dir / "done"
    for f in sorted(s.acks_dir.glob("*.json")):
        try:
            ack = json.loads(f.read_text("utf-8"))
        except ValueError:
            log.error("битый ack %s, пропускаю", f.name)
            continue
        key = ack["key"]
        ev = _apply_ack(s, db, ack) if ack["kind"] == "apply" else _rollback_ack(db, ack) if ack["kind"] == "rollback" else None
        if ev is not None:  # повторный ack (ноут успел ответить дважды) событий не даёт: карточка не дублируется
            events.append(ev)
        (s.outbox_dir / f"{key}.json").unlink(missing_ok=True)  # ноут ответил, запрос больше не нужен
        shutil.rmtree(s.outbox_dir / key, ignore_errors=True)
        done_dir.mkdir(exist_ok=True)
        os.replace(f, done_dir / f.name)
    return events


def _apply_ack(s: Settings, db: DB, ack: dict) -> dict | None:
    did = int(ack["key"].split("-", 1)[1])
    d = db.get_draft(did)
    if d is None:
        return None
    if ack["ok"]:
        if d["status"] == "written":
            return None
        db.put_block(ack["block"])
        db.update_draft(did, status="written")
        db.update_post(d["post_id"], status="written")
        drop_keep(s, d["post_id"])
        return {"type": "written", "draft_id": did, "block_id": ack["block"]["id"], "path": ack["block"]["path"]}
    if d["status"] != "queued":
        return None
    db.update_draft(did, status="sent")  # кнопки снова рабочие: можно сменить цель или повторить ✅
    return {"type": "failed", "draft_id": did, "error": ack.get("error", "")}


def _rollback_ack(db: DB, ack: dict) -> dict | None:
    bid, res = int(ack["block_id"]), ack.get("res", "")
    b = db.get_block(bid)
    if ack["ok"] and res in ("ok", "already"):
        if b is not None and b["rolled_back"]:
            return None
        db.mark_rolled_back(bid)
        if b is not None:
            db.update_draft(b["draft_id"], status="rolled_back")
        db.c.execute("UPDATE audits SET status='rolled_back' WHERE block_id=?", (bid,))
    return {"type": "rolled_back", "block_id": bid, "res": res if ack["ok"] else "error", "error": ack.get("error", "")}
