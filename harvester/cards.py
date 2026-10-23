"""Тексты и клавиатуры бота. Чистые функции без сети."""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as K

MAX = 3500
PHASES = {"new": "новое", "retro": "ретро", "done": "готово"}


def _trim(t: str, n: int = MAX) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


def _stem(rel: str) -> str:
    return PurePosixPath(rel).stem


def _with_footer(body: str, footer: str, link: str, n: int = MAX) -> str:
    """Тело карточки первым, источник и ссылка на пост в конце: заголовок не отнимает внимание сверху."""
    return f"{_trim(body, n - len(footer) - len(link) - 8)}\n\n{footer}\n{link}"


def where(mode: str, target: str, title: str | None = None) -> str:
    if mode == "append":
        return f"→ дописать в [[{_stem(target)}]]"
    return f"→ новая заметка в «{target}»" + (f": {title}" if title else "")


def draft_card(d, post, channel: str, link: str) -> str:
    return _with_footer(f"{where(d['mode'], d['target'], d['title'])}\n\n{d['content']}",
                        f"📌 {channel} · {post['date'][:10]}", link)


def draft_kb(did: int, link: str) -> K:
    return K(inline_keyboard=[
        [B(text="✅", callback_data=f"ok:{did}"), B(text="❌", callback_data=f"no:{did}"),
         B(text="✏️ другая", callback_data=f"alt:{did}")],
        [B(text="🔗 пост", url=link)],
    ])


def alts_kb(did: int, alts: list) -> K:
    rows = [[B(text=_trim(f"дописать: {_stem(t)}" if m == "append" else f"новая в «{t}»", 60),
               callback_data=f"to:{did}:{i}")] for i, (m, t) in enumerate(alts)]
    rows.append([B(text="↩️ назад", callback_data=f"back:{did}")])
    return K(inline_keyboard=rows)


def written_kb(bid: int) -> K:
    return K(inline_keyboard=[[B(text="↩️ Откатить", callback_data=f"rb:{bid}")]])


def confirm_kb(bid: int) -> K:
    return K(inline_keyboard=[[B(text="Да, откатить", callback_data=f"rby:{bid}"),
                               B(text="Отмена", callback_data=f"rbn:{bid}")]])


def video_card(post, channel: str, link: str) -> str:
    return _with_footer(post["text"], f"🎬 {channel} · {post['date'][:10]} · разобрать видео?", link, 1500)


def video_kb(pid: int) -> K:
    return K(inline_keyboard=[[B(text="🎬 Разобрать", callback_data=f"vy:{pid}"),
                               B(text="❌ Не надо", callback_data=f"vn:{pid}")]])


REASONS = {1: "неинтересно", 2: "дубль", 3: "реклама"}


def reason_kb(pid: int) -> K:
    return K(inline_keyboard=[[B(text="😴 неинтересно", callback_data=f"vr:{pid}:1"),
                               B(text="🔁 дубль", callback_data=f"vr:{pid}:2"),
                               B(text="📢 реклама", callback_data=f"vr:{pid}:3")]])


def more_text(shown: int, left: int, step: int) -> str:
    return f"Показал {shown} видео за сегодня, в очереди ещё {left}. Полистать ещё {step}?"


def more_kb() -> K:
    return K(inline_keyboard=[[B(text="Да", callback_data="mv:1"), B(text="Нет", callback_data="mv:0")]])


def pilot_card(post, channel: str, link: str) -> str:
    body = "\n".join(x for x in (post["text"], post["extra_text"]) if x)
    return _with_footer(body, f"🧪 {channel} · совпадение {post['score']:.2f}", link, 1500)


def pilot_kb(pid: int) -> K:
    return K(inline_keyboard=[[B(text="👍 полезно", callback_data=f"pl:{pid}:1"),
                               B(text="👎 мусор", callback_data=f"pl:{pid}:0")]])


def audit_card(audit, block) -> str:
    extra = json.loads(block["extra"])
    added = extra.get("line") or extra.get("item") or ""
    return _trim(f"🔍 Аудит: сомнительный блок в «{block['path']}»\nПричина: {audit['reason']}\n\n{added}")


def audit_kb(aid: int) -> K:
    return K(inline_keyboard=[[B(text="↩️ Откатить", callback_data=f"ab:{aid}"),
                               B(text="Оставить", callback_data=f"ak:{aid}")]])


def night_text(r: dict) -> str:
    text = (f"🌙 Ночь {r['date']}: вызовов Claude {r['calls']}/{r['limit']}, в очереди было {r['queue']}, "
            f"черновиков {r['drafted']}, отсеяно {r['skipped']}, ошибок {r['errors']}")
    if r.get("retro"):
        text += "\nИдёт догон архива, лимит повышен"
    if r.get("aborted"):
        text += f"\n⛔ Прервано: {r['aborted']}"
    return text


def audit_text(r: dict) -> str:
    text = f"🔍 Аудит {r['date']}: проверено {r['checked']}, сомнительных {r['doubt']}, вызовов {r['calls']}/{r['limit']}"
    return text + (f"\n⛔ Прервано: {r['aborted']}" if r.get("aborted") else "")


def status_text(counts: dict, phases: list[tuple[str, str]]) -> str:
    lines = ["📊 Посты: " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "пока пусто")]
    lines += [f"{name}: {PHASES.get(ph, ph)}" for name, ph in phases]
    return "\n".join(lines)
