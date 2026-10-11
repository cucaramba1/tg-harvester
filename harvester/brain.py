"""Ночной разбор кандидатов через claude -p с жёстким лимитом вызовов."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import Settings
from .db import DB
from .files import drop_keep, list_sections


class BrainError(RuntimeError):
    pass


SYSTEM = """Ты отбираешь посты из Telegram-каналов для Obsidian Некита. Его vault это список «изучить и использовать позже», разложенный по темам. Отвечай по-русски, строго по JSON-схеме, по одному элементу на каждый post_id.

Ценно: практическая польза потом. Гайд, инструкция, промпт, инструмент или сервис со ссылкой, схема заработка, цифры и выводы, рекомендация книги или курса, упражнение или полезная привычка.
Не ценно: новости, мемы, реклама, мотивационные цитаты без сути, анонсы, розыгрыши. Для них mode skip.

mode:
- append: дописать в существующую заметку. target: точный путь из «Похожие заметки» этого поста. Только если пост прямо по теме этой заметки.
- new: новая заметка. target: точное имя раздела из списка разделов. title: содержательный заголовок до 80 символов.
- skip: не ценно, reason обязателен.

content: что записать. Для append одна-три строки сути. Для new тело заметки в markdown без заголовка верхнего уровня. Промпты и команды переноси дословно в блоке кода. Ссылки из поста сохраняй. Не выдумывай ничего, чего нет в посте. Не используй длинное тире, пиши через запятую, двоеточие или дефис.
keep_image: true, только если картинка поста сама несёт суть (схема, скрин интерфейса, таблица).
Цели Некита даны, чтобы понимать, что ему важно. Полезный для целей пост вне текущих тем бери как new в ближайший по смыслу раздел."""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "post_id": {"type": "integer"},
            "mode": {"type": "string", "enum": ["append", "new", "skip"]},
            "target": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "content": {"type": "string"},
            "reason": {"type": "string"},
            "keep_image": {"type": "boolean"},
        },
        "required": ["post_id", "mode", "target", "title", "content", "reason", "keep_image"],
    }}},
    "required": ["items"],
}


async def run_claude(s: Settings, prompt: str, *, system: str, schema: dict | None = None,
                     tools: tuple[str, ...] = (), add_dirs: tuple[str, ...] = (), timeout: int) -> dict | str:
    args = [s.claude_bin, "-p", "--output-format", "json", "--no-session-persistence",
            "--system-prompt", system, "--tools", ",".join(tools)]
    if tools:
        args += ["--allowedTools", ",".join(tools)]  # без этого в -p тул упрётся в запрос разрешения
    for d in add_dirs:
        args += ["--add-dir", d]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    s.data_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": f"{os.path.dirname(s.claude_bin)}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(s.data_dir), env=env)
    except OSError as e:
        raise BrainError(f"не смог запустить claude: {e}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BrainError(f"claude не ответил за {timeout} с")
    if proc.returncode != 0:
        raise BrainError(f"claude exit {proc.returncode}: {(err or out).decode(errors='ignore')[-300:]}")
    try:
        data = json.loads(out)
    except ValueError:
        raise BrainError("claude вернул не JSON")
    if data.get("is_error"):
        raise BrainError(f"claude: {str(data.get('result'))[:300]}")
    if schema is not None:
        so = data.get("structured_output")
        if not isinstance(so, dict):
            raise BrainError("claude не вернул structured_output")
        return so
    return str(data.get("result") or "")


@dataclass
class Verdict:
    post_id: int
    mode: str
    target: str | None
    title: str | None
    content: str
    reason: str
    keep_image: bool


def chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def demote(md: str) -> str:
    """Заголовки # и ## из ответа -> ###: иначе сломают структуру заметки."""
    return re.sub(r"(?m)^#{1,2} ", "### ", md)


def is_limit(e: Exception) -> bool:
    """Лимит подписки или claude вообще не запускается: ночь обрывается, посты ждут без штрафа."""
    t = str(e).lower()
    return any(w in t for w in ("limit", "лимит", "usage", "не смог запустить"))


def _safe_note(vault: Path, rel: str | None) -> bool:
    if not rel:
        return False
    p = (vault / rel).resolve()
    return p.is_relative_to(vault.resolve()) and p.is_file()


def _top_sections(post) -> list[str]:
    return [r.split("/")[0] for r, _ in json.loads(post["top3"]) if "/" in r]


def fallback_section(post, sections: list[str]) -> str | None:
    cands = ([post["target"]] if post["mode"] == "new" and post["target"] else []) + _top_sections(post)
    return next((c for c in cands if c in sections), None)


def alternatives(post, sections: list[str], vault: Path) -> list[list[str]]:
    alts = [["append", r] for r, _ in json.loads(post["top3"]) if _safe_note(vault, r)]
    extra = _top_sections(post) + ([post["target"]] if post["mode"] == "new" and post["target"] else [])
    for sec in extra:
        if sec in sections and ["new", sec] not in alts:
            alts.append(["new", sec])
    return alts[:6]


def validate(items: list[dict], posts: list, sections: list[str], vault: Path) -> dict[int, Verdict]:
    by_id = {p["id"]: p for p in posts}
    out: dict[int, Verdict] = {}
    for it in items:
        pid = it.get("post_id")
        if pid not in by_id or pid in out:
            continue
        p = by_id[pid]
        content = demote(str(it.get("content") or "").strip())
        reason = str(it.get("reason") or "").strip()
        keep = bool(it.get("keep_image"))
        mode, target = it.get("mode"), it.get("target")
        if mode not in ("append", "new") or not content:
            out[pid] = Verdict(pid, "skip", None, None, "", reason or "не ценно", False)
            continue
        allowed = [r for r, _ in json.loads(p["top3"])]
        if mode == "append" and target in allowed and _safe_note(vault, target):
            out[pid] = Verdict(pid, "append", target, None, content, reason, keep)
            continue
        if not (mode == "new" and target in sections):
            target = fallback_section(p, sections)
        if target is None:
            out[pid] = Verdict(pid, "skip", None, None, "", "некуда положить", False)
            continue
        title = (str(it.get("title") or "").strip() or content.splitlines()[0]).lstrip("#-* ").strip()[:80]
        out[pid] = Verdict(pid, "new", target, title, content, reason, keep)
    return out


def build_prompt(posts: list, sections: list[str], goals: str, channel_name, images: list[tuple[int, str]]) -> str:
    parts = ["## Разделы vault", ", ".join(sections)]
    if goals:
        parts += ["", "## Цели Некита (кратко)", goals]
    parts += ["", "## Посты"]
    for p in posts:
        top = "\n".join(f"  - {r} ({sc})" for r, sc in json.loads(p["top3"])) or "  (нет)"
        text = p["text"] or ""
        if p["extra_text"]:
            text += "\n[текст с картинок и аудио]\n" + p["extra_text"]
        parts += [f"### post_id {p['id']} · {channel_name(p['chat_id'])} · {p['date'][:10]}",
                  f"Похожие заметки:\n{top}", text.strip()[:4000], ""]
    if images:
        parts += ["## Картинки (посмотри тулом Read)", *[f"post_id {pid}: {path}" for pid, path in images]]
    return "\n".join(parts)


async def run_night(s: Settings, db: DB, claude=run_claude) -> dict:
    retro = db.retro_active()
    limit = int(s.max_calls_per_night * (s.retro_multiplier if retro else 1))
    queue = db.night_queue(s.explore_quota)
    sections = list_sections(s.vault)
    goals_p = s.vault / s.goals_note
    goals = goals_p.read_text("utf-8")[:3000] if goals_p.exists() else ""
    rep = {"date": date.today().isoformat(), "limit": limit, "calls": 0, "queue": len(queue), "drafted": 0,
           "skipped": 0, "errors": 0, "aborted": None, "retro": retro}
    images_left = s.max_images_per_night
    for batch in chunks(queue, s.batch_size):
        if rep["calls"] >= limit:
            break
        imgs: list[tuple[int, str]] = []
        for p in batch:
            if images_left and not (p["extra_text"] or "").strip():
                path = next((x for x in json.loads(p["images"]) if Path(x).exists()), None)
                if path:
                    imgs.append((p["id"], path))
                    images_left -= 1
        prompt = build_prompt(batch, sections, goals, s.channel_name, imgs)
        kw = dict(system=SYSTEM, schema=SCHEMA, tools=("Read",) if imgs else (),
                  add_dirs=tuple(sorted({str(Path(x).parent) for _, x in imgs})), timeout=s.claude_timeout)
        raw = None
        for _ in range(2):
            if rep["calls"] >= limit:
                break
            rep["calls"] += 1
            try:
                raw = await claude(s, prompt, **kw)
                break
            except BrainError as e:
                if is_limit(e):
                    rep["aborted"] = str(e)[:200]
                    break
        if rep["aborted"]:
            break
        verdicts = validate(raw.get("items") or [], batch, sections, s.vault) if isinstance(raw, dict) else {}
        for p in batch:
            v = verdicts.get(p["id"])
            if v is None:
                if db.bump_attempts(p["id"]) >= 3:
                    db.update_post(p["id"], status="error", reason="claude: нет ответа по посту")
                    drop_keep(s, p["id"])
                    rep["errors"] += 1
            elif v.mode == "skip":
                db.update_post(p["id"], status="rejected", reason=f"ai: {v.reason}"[:300])
                drop_keep(s, p["id"])
                rep["skipped"] += 1
            else:
                db.add_draft(p["id"], v.mode, v.target, v.title, v.content, v.keep_image,
                             alternatives(p, sections, s.vault))
                db.update_post(p["id"], status="drafted")
                rep["drafted"] += 1
    if rep["aborted"]:
        db.push_alert(f"⛔ Ночь прервана: {rep['aborted']}")
    db.kv_set("night_report", json.dumps(rep, ensure_ascii=False))
    return rep
