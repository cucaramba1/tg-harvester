"""Запись одобренных черновиков в vault и откат. Существующий текст заметки не меняется никогда."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .config import Settings
from .db import DB
from .files import drop_keep, find_moc, snapshot
from .vault.canvas import Canvas, add_to_group, ensure_side_group
from .vault.fs import atomic_write, wikilink
from .vault.notes import moc_add, moc_remove, unique_note_path

TG_H = "## Из Telegram"
TG_GROUP = "📥 Из Telegram"
MOC_SECTION = "Из Telegram"


class WriteError(RuntimeError):
    pass


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def post_link(chat_id: int, msg_id: int) -> str:
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{msg_id}"


def render_line(content: str, link: str, day: str) -> str:
    lines = [x.rstrip() for x in content.strip().splitlines()] or ["(пусто)"]
    src = f"([пост]({link}), {day})"
    if len(lines) == 1:
        return f"- {lines[0]} {src}\n"
    body = "\n".join(f"  {x}" if x else "" for x in lines[1:])
    return f"- {lines[0]}\n{body}\n  {src}\n"


def plan_append(text: str, line: str) -> tuple[int, str]:
    """Куда вставить и что. Всё выше точки вставки остаётся как было."""
    m = re.search(rf"(?m)^{re.escape(TG_H)}[ \t]*$", text)
    if m is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        return len(text), f"{sep}\n{TG_H}\n\n{line}"
    nxt = re.search(r"(?m)^## ", text[m.end():])
    if nxt:
        return m.end() + nxt.start(), line + "\n"
    sep = "" if text.endswith("\n") else "\n"
    return len(text), sep + line


def inside(vault: Path, rel: str) -> Path:
    """Путь внутри vault или WriteError: Claude не должен уметь писать куда-то ещё."""
    if not rel or not (vault / rel).resolve().is_relative_to(vault.resolve()) or (vault / rel).resolve() == vault.resolve():
        raise WriteError(f"путь вне vault: {rel}")
    return vault / rel


class Writer:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db

    def apply(self, did: int) -> int:
        d = self.db.get_draft(did)
        if d is None or d["status"] not in ("drafted", "sent"):
            raise WriteError("черновик уже обработан")
        post = self.db.get_post(d["post_id"])
        link, day = post_link(post["chat_id"], post["msg_id"]), post["date"][:10]
        if d["mode"] == "append":
            path = inside(self.s.vault, d["target"])
            if not path.is_file():
                raise WriteError(f"заметки больше нет: {d['target']}")
        else:
            path = inside(self.s.vault, d["target"])
            if not path.is_dir():
                raise WriteError(f"раздела нет: {d['target']}")
        image = self._copy_image(post) if d["keep_image"] else None
        content = d["content"].strip() + (f"\n![[{image}]]" if image else "")
        try:
            if d["mode"] == "append":
                bid = self._append(d, path, content, link, day, image)
            else:
                bid = self._new(d, post, path, content, link, day, image)
        except Exception:
            if image:
                (self.s.vault / image).unlink(missing_ok=True)
            raise
        self.db.update_draft(did, status="written")
        self.db.update_post(post["id"], status="written")
        drop_keep(self.s, post["id"])
        return bid

    def _copy_image(self, post) -> str | None:
        for p in map(Path, json.loads(post["images"])):
            if p.exists():
                rel = f"{self.s.attachments_dir}/tg-{str(post['chat_id']).removeprefix('-100')}-{post['msg_id']}{p.suffix}"
                dest = self.s.vault / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
                return rel
        return None

    def _append(self, d, path: Path, content: str, link: str, day: str, image: str | None) -> int:
        snapshot(self.s, path)
        text = path.read_bytes().decode("utf-8")
        line = render_line(content, link, day)
        pos, ins = plan_append(text, line)
        atomic_write(path, text[:pos] + ins + text[pos:])
        start, size = len(text[:pos].encode()), len(ins.encode())
        self.db.shift_blocks(d["target"], start, size)
        extra = {"text": ins, "line": line, **({"image": image} if image else {})}
        return self.db.add_block(d["id"], d["target"], "append", start, start + size, sha(ins.encode()), extra)

    def _new(self, d, post, folder: Path, content: str, link: str, day: str, image: str | None) -> int:
        section = d["target"]
        title = (d["title"] or "Из Telegram").strip()
        path = unique_note_path(folder, title)
        rel = path.relative_to(self.s.vault).as_posix()
        related = [wikilink(r) for r, _ in json.loads(post["top3"]) if (self.s.vault / r).is_file()][:3]
        channel = self.s.channel_name(post["chat_id"])
        body = (f"---\nsource: {json.dumps(link)}\nchannel: {json.dumps(channel, ensure_ascii=False)}\n"
                f"date: {day}\ntags: [telegram]\n---\n# {title}\n\n{content}\n\nИсточник: [пост]({link}), {channel}\n")
        if related:
            body += "\n## Связанные заметки\n" + "".join(f"- {x}\n" for x in related)
        atomic_write(path, body)
        extra: dict = {"item": wikilink(rel), **({"image": image} if image else {})}
        if moc := find_moc(self.s.vault, section):
            snapshot(self.s, moc)
            moc_add(moc, MOC_SECTION, wikilink(rel))
            extra["moc"] = moc.relative_to(self.s.vault).as_posix()
        cv_path = self.s.vault / f"{section}.canvas"
        if cv_path.exists():
            snapshot(self.s, cv_path)
            cv = Canvas.load(cv_path)
            node = add_to_group(cv, ensure_side_group(cv, TG_GROUP), rel)
            cv.save()
            extra["canvas"], extra["node"] = cv_path.name, node["id"]
        data = path.read_bytes()
        return self.db.add_block(d["id"], rel, "new", 0, len(data), sha(data), extra)

    def rollback(self, bid: int) -> str:
        b = self.db.get_block(bid)
        if b is None or b["rolled_back"]:
            return "already"
        extra = json.loads(b["extra"])
        path = self.s.vault / b["path"]
        res = self._rollback_append(b, path, extra) if b["kind"] == "append" else self._rollback_new(b, path, extra)
        if res == "ok":
            if img := extra.get("image"):
                (self.s.vault / img).unlink(missing_ok=True)
            self.db.mark_rolled_back(bid)
            self.db.update_draft(b["draft_id"], status="rolled_back")
        return res

    def _rollback_append(self, b, path: Path, extra: dict) -> str:
        if not path.exists():
            return "gone"
        raw = path.read_bytes()
        ins, line = extra["text"].encode(), extra["line"].encode()
        start = b["start"]
        if raw[start:start + len(ins)] != ins:
            if raw.count(ins) != 1:
                return "changed"
            start = raw.index(ins)
        cut_from, cut_to = start, start + len(ins)
        later = [o for o in self.db.active_blocks(b["path"]) if o["id"] != b["id"] and o["start"] >= b["stop"]]
        if later and ins != line and ins.endswith(line):  # заголовок создал этот блок, но под ним уже есть другие
            cut_from = cut_to - len(line)
        snapshot(self.s, path)
        atomic_write(path, (raw[:cut_from] + raw[cut_to:]).decode("utf-8"))
        self.db.shift_blocks(b["path"], cut_to, -(cut_to - cut_from))
        return "ok"

    def _rollback_new(self, b, path: Path, extra: dict) -> str:
        if path.exists():
            if sha(path.read_bytes()) != b["sha"]:
                return "changed"
            snapshot(self.s, path)
            path.unlink()
        if moc_rel := extra.get("moc"):
            moc_remove(self.s.vault / moc_rel, extra["item"])
        if cv_name := extra.get("canvas"):
            cv_path = self.s.vault / cv_name
            if cv_path.exists():
                cv = Canvas.load(cv_path)
                cv.remove_node(extra["node"])
                g = cv.group(TG_GROUP)
                if g and not cv.children(g):
                    cv.remove_node(g["id"])
                cv.save()
        return "ok"
