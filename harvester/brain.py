"""Ночной разбор кандидатов через claude -p с жёстким лимитом вызовов."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import Settings, as_roots, locate, tool_path
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
    args = [s.claude_bin, "-p", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--output-format", "json", "--no-session-persistence",
            "--system-prompt", system, "--tools", ",".join(tools)]
    if tools:
        args += ["--allowedTools", ",".join(tools)]  # без этого в -p тул упрётся в запрос разрешения
    args += ["--model", os.environ.get("CLAUDE_BATCH_MODEL", "sonnet")]  # ночные батчи не на Opus
    for d in add_dirs:
        args += ["--add-dir", d]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    s.data_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": tool_path(os.path.dirname(s.claude_bin))}
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


ROUTER_URL = os.environ.get("FREELLM_URL", "http://127.0.0.1:3001")
ROUTER_DB = Path.home() / "Claude Code/freellmapi/server/data/freeapi.db"
# на 99 постах gemini-3.8-flash совпал 82/99 против 88 у sonnet; у одной модели один маршрут, при лимите идём на auto
ROUTER_MODELS = tuple(m.strip() for m in os.environ.get("FREELLM_MODEL", "gemini-3.8-flash,auto").split(",") if m.strip())


def _router_key() -> str:
    if os.environ.get("FREELLM_KEY"):
        return os.environ["FREELLM_KEY"]
    import sqlite3
    try:
        with sqlite3.connect(f"file:{ROUTER_DB}?mode=ro", uri=True) as c:
            row = c.execute("select value from settings where key='unified_api_key'").fetchone()
    except sqlite3.Error as e:
        raise BrainError(f"ключ роутера недоступен: {e}")
    if not row:
        raise BrainError("ключ роутера не найден")
    return row[0]


async def run_router(s: Settings, prompt: str, *, system: str, schema: dict | None = None,
                     tools: tuple[str, ...] = (), add_dirs: tuple[str, ...] = (), timeout: int) -> dict | str:
    """Запас на случай лимита Claude: локальный FreeLLMAPI. Картинки (tools) не читает."""
    import urllib.request

    hint = ("\n\nОтветь ТОЛЬКО JSON без markdown-ограждений, строго по схеме:\n"
            + json.dumps(schema, ensure_ascii=False)) if schema else ""
    key = _router_key()

    def call(model: str) -> str:
        body = json.dumps({"model": model, "max_tokens": 8000, "temperature": 0,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt + hint}]}).encode()
        rq = urllib.request.Request(f"{ROUTER_URL}/v1/chat/completions", body,
                                    {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.load(r)["choices"][0]["message"]["content"] or ""

    def first_working() -> str:
        err: Exception | None = None
        for model in ROUTER_MODELS:
            try:
                return call(model)
            except Exception as e:  # лимит или недоступный маршрут: пробуем следующую модель
                err = e
        raise err or RuntimeError("модели роутера не заданы")

    try:
        txt = await asyncio.to_thread(first_working)
    except Exception as e:
        raise BrainError(f"роутер недоступен: {str(e)[:200]}")
    if schema is None:
        return txt
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        raise BrainError("роутер вернул не JSON")


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


# Формулировки из логов (You've hit your session/weekly limit · resets ...) и из бинаря claude 25.10.2026
_LIMIT = re.compile(r"hit your \w+ limit|limit reached|reached your \w+ (usage )?limit|out of extra usage|лимит|не смог запустить")


def is_limit(e: Exception) -> bool:
    """Лимит подписки или claude вообще не запускается: ночь обрывается, посты ждут без штрафа."""
    t = str(e).lower()
    if "context limit" in t or "usage:" in t:  # переполнен контекст / справка CLI при кривых флагах: это не лимит подписки
        return False
    return bool(_LIMIT.search(t))


def _safe_note(vault: Path, rel: str | None) -> bool:
    if not rel:
        return False
    p = locate(vault, rel).resolve()
    return any(p.is_relative_to(r.resolve()) for r in as_roots(vault)) and p.is_file()


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


async def run_night(s: Settings, db: DB, claude=run_claude, router=run_router) -> dict:
    retro = db.retro_active()
    limit = int(s.max_calls_per_night * (s.retro_multiplier if retro else 1))
    queue = db.night_queue(s.explore_quota, s.night_section_cap, s.explore_keywords)
    sections = list_sections(s.vaults)
    goals_p = s.vault / s.goals_note
    goals = goals_p.read_text("utf-8")[:3000] if goals_p.exists() else ""
    rep = {"date": date.today().isoformat(), "limit": limit, "calls": 0, "queue": len(queue), "drafted": 0,
           "skipped": 0, "errors": 0, "aborted": None, "retro": retro}
    images_left = s.max_images_per_night
    on_router = False
    tail = s.router_tail_batches if s.router_fallback else 0  # батчи сверх лимита Claude уходят в бесплатный роутер
    cap = limit + tail
    for batch in chunks(queue, s.batch_size):
        if rep["calls"] >= cap:
            break
        imgs: list[tuple[int, str]] = []
        for p in batch:
            if images_left and not on_router and rep["calls"] < limit and not (p["extra_text"] or "").strip():
                path = next((x for x in json.loads(p["images"]) if Path(x).exists()), None)
                if path:
                    imgs.append((p["id"], path))
                    images_left -= 1
        prompt = build_prompt(batch, sections, goals, s.channel_name, imgs)
        kw = dict(system=SYSTEM, schema=SCHEMA, tools=("Read",) if imgs else (),
                  add_dirs=tuple(sorted({str(Path(x).parent) for _, x in imgs})), timeout=s.claude_timeout)
        raw = None
        in_tail = False
        for _ in range(2):
            if rep["calls"] >= cap:
                break
            rep["calls"] += 1
            if rep["calls"] > limit:
                on_router = in_tail = True
                rep["tail"] = rep.get("tail", 0) + 1
            try:
                if on_router:  # без целей Некита (личное не уходит во внешние бесплатные тиры) и без картинок
                    raw = await router(s, build_prompt(batch, sections, "", s.channel_name, []),
                                       system=SYSTEM, schema=SCHEMA, timeout=s.claude_timeout)
                else:
                    raw = await claude(s, prompt, **kw)
                break
            except BrainError as e:
                if in_tail or on_router:  # роутер (хвост или замена при лимите) упал: без штрафа постам, ночь дальше не тянем
                    rep["tail_error"] = str(e)[:200]
                    break
                if is_limit(e) and s.router_fallback and not on_router:
                    on_router = True
                    rep["fallback"] = str(e)[:200]
                    continue
                if is_limit(e):
                    rep["aborted"] = str(e)[:200]
                    break
        if rep["aborted"] or rep.get("tail_error"):
            break
        verdicts = validate(raw.get("items") or [], batch, sections, s.vaults) if isinstance(raw, dict) else {}
        for p in batch:
            v = verdicts.get(p["id"])
            if v is None:
                if in_tail or on_router:
                    continue  # бесплатная модель могла промолчать: пост остаётся в очереди для Claude без штрафа
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
                             alternatives(p, sections, s.vaults))
                db.update_post(p["id"], status="drafted")
                rep["drafted"] += 1
    if rep["aborted"]:
        db.push_alert(f"⛔ Ночь прервана: {rep['aborted']}")
    db.kv_set("night_report", json.dumps(rep, ensure_ascii=False))
    return rep
