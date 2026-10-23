"""Воскресный аудит: Claude перечитывает блоки за неделю и отмечает сомнительные."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .brain import BrainError, chunks, run_claude
from .config import Settings
from .db import DB

AUDIT_SYSTEM = """Ты проверяешь, что бот дописал в заметки Obsidian за неделю. Для каждого блока verdict ok, если блок по теме заметки, не повторяет то, что в заметке уже есть, и не обрывается на полуслове. Иначе verdict doubt и короткий reason по-русски. Не используй длинное тире. Строго по JSON-схеме."""

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"block_id": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["ok", "doubt"]},
                       "reason": {"type": "string"}},
        "required": ["block_id", "verdict", "reason"],
    }}},
    "required": ["items"],
}


def block_view(s: Settings, b) -> str | None:
    path = s.path(b["path"])
    if not path.exists():
        return None
    note = path.read_text("utf-8", errors="ignore")
    extra = json.loads(b["extra"])
    if b["kind"] == "append":
        before = note.replace(extra.get("text", ""), "", 1)
        return (f"### block_id {b['id']} · дописано в «{b['path']}»\nЗаметка без блока:\n{before[:3000]}\n\n"
                f"Дописанный блок:\n{extra.get('line', '')}")
    return f"### block_id {b['id']} · новая заметка «{b['path']}»\n{note[:3000]}"


async def run_audit(s: Settings, db: DB, claude=run_claude, days: int = 7, now=datetime.now) -> dict:
    since = (now() - timedelta(days=days)).isoformat(timespec="seconds")
    limit = max(1, s.max_calls_per_night // 2)
    rep = {"date": now().date().isoformat(), "checked": 0, "doubt": 0, "calls": 0, "limit": limit, "aborted": None}
    for chunk in chunks(db.blocks_since(since), 10):
        if rep["calls"] >= limit:
            break
        views = [(b, v) for b in chunk if (v := block_view(s, b))]
        if not views:
            continue
        rep["calls"] += 1
        try:
            raw = await claude(s, "\n\n".join(v for _, v in views), system=AUDIT_SYSTEM, schema=AUDIT_SCHEMA,
                               timeout=s.claude_timeout)
        except BrainError as e:
            rep["aborted"] = str(e)[:200]
            break
        ids = {b["id"] for b, _ in views}
        for it in (raw.get("items") if isinstance(raw, dict) else None) or []:
            if it.get("block_id") not in ids:
                continue
            rep["checked"] += 1
            if it.get("verdict") == "doubt":
                db.add_audit(it["block_id"], "doubt", str(it.get("reason") or "")[:300])
                rep["doubt"] += 1
    db.kv_set("audit_report", json.dumps(rep, ensure_ascii=False))
    return rep
