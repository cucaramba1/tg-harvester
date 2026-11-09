"""Тексты и клавиатуры бота. Чистые функции без сети."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as K

MAX = 3500
PHASES = {"new": "новое", "retro": "ретро", "done": "готово"}


def _trim(t: str, n: int = MAX) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


def _trim_sentence(t: str, n: int) -> str:
    """Длинный черновик режем по концу предложения или абзаца, а не посреди слова (идея Telebrief);
    в заметку всё равно уйдёт полный текст."""
    if len(t) <= n:
        return t
    tail = " … (полностью в заметке)"
    cut = t[: n - len(tail)]
    ends = [m.end() for m in re.finditer(r"[.!?…](\s|$)|\n", cut)]
    if ends and ends[-1] > len(cut) * 0.6:
        cut = cut[: ends[-1]]
    return cut.rstrip() + tail


# цифры (с пробелами-разделителями), ссылки, @ники и латинские слова: то, что модель чаще всего выдумывает
_FACT = re.compile(r"https?://\S+|@\w{3,}|\d+(?:[ .,]\d+)*%?|\b[A-Za-z][A-Za-z0-9.+-]{2,}\b")
# служебное из markdown и кода плюс контекст Некита, который подмешивается из целей, а не из поста
_FACT_SKIP = {"text", "url", "bash", "python", "json", "markdown", "http", "https", "open", "source", "mini-app",
              "cucaramba", "telegram", "obsidian", "claude"}


def unverified(content: str, source: str) -> list[str]:
    """Факты из черновика, которых нет в исходном посте (вместо гейта nyan ads.py: у нас пересказ, а не чистка)."""
    src = re.sub(r"\s", "", source.lower())
    body = re.sub(r"(?s)```.*?```|`[^`]*`|\[\[[^\]]*\]\]", " ", content)  # код, команды и [[ссылки на заметки]] не факты поста
    body = re.sub(r"(?m)^\s*\d+[.)]\s", " ", body)  # нумерация списка
    out = []
    for m in _FACT.finditer(body):
        tok = m.group(0).rstrip(".,")
        key = re.sub(r"\s", "", tok.lower())
        if len(key) < 2 or key in _FACT_SKIP or key in src or tok in out:
            continue
        out.append(tok)
    return out


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
    footer = f"📌 {channel} · {post['date'][:10]}"
    new = unverified(d["content"] or "", f"{post['text'] or ''}\n{post['extra_text'] or ''}")
    if new:
        footer = f"⚠ проверь, в посте этого нет: {_trim(', '.join(new[:6]), 200)}\n{footer}"
    head = f"{where(d['mode'], d['target'], d['title'])}\n\n"
    return f"{head}{_trim_sentence(d['content'] or '', MAX - len(head) - len(footer) - len(link) - 8)}\n\n{footer}\n{link}"


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


REJECT_REASONS = {1: "не по теме", 2: "дубль", 3: "слишком общее", 4: "не туда записал"}


def reject_kb(did: int) -> K:
    row = [B(text=t, callback_data=f"nr:{did}:{i}") for i, t in REJECT_REASONS.items()]
    return K(inline_keyboard=[row[:2], row[2:], [B(text="без причины", callback_data=f"nr:{did}:0")]])


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


def topic_card(topic: str, n: int, files: int, samples: list[str]) -> str:
    dup = f", из них повторы одного файла: {n - files}" if files < n else ""
    lines = [f"🧩 {topic}", f"{n} видео в очереди{dup}", "", "Примеры подписей:"]
    lines += [f" • {_trim(x.strip().splitlines()[0] if x.strip() else '(без подписи)', 90)}" for x in samples[:3]]
    return "\n".join(lines)


def topic_kb(tid: int, with_samples: bool = True) -> K:
    row = [B(text="🔬 Проверить тему", callback_data=f"tc:{tid}")]
    if with_samples:
        row.append(B(text="👀 Показать 3", callback_data=f"t3:{tid}"))
    row.append(B(text="🚫 Тему не надо", callback_data=f"tx:{tid}"))
    return K(inline_keyboard=[row])


def topic_report_card(rep: dict) -> str:
    lines = [f"🧩 {rep['topic']}: итог проверки",
             f"{rep['total']} видео, разных файлов {rep['files']}, "
             f"разных упражнений и советов {len(rep['groups'])}, без описания {len(rep['undescribed'])}", ""]
    for i, g in enumerate(rep["groups"], 1):
        times = f" (×{g['count']})" if g["count"] > 1 else ""
        lines.append(f"{i}. {g['title']}{times}" + (f": {g['note']}" if g["note"] else ""))
    if rep["undescribed"]:
        lines += ["", "Без описания (ни речи, ни текста, смотреть самому):"]
        lines += [f" • {_trim(u['caption'] or '(без подписи)', 90)}" for u in rep["undescribed"][:8]]
    return _trim("\n".join(lines))


def topic_report_kb(tid: int) -> K:
    return K(inline_keyboard=[[B(text="📝 Записать в заметку", callback_data=f"tw:{tid}"),
                               B(text="🗑 Не надо", callback_data=f"tn:{tid}")]])


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


def eval_card(d, post, channel: str, link: str, i: int, n: int) -> str:
    return f"🧪 Проверка фильтра {i}/{n}: писать такое в Obsidian автоматом?\n\n" + draft_card(d, post, channel, link)


def eval_kb(pid: int, link: str) -> K:
    return K(inline_keyboard=[[B(text="👍 писать", callback_data=f"ev:{pid}:1"), B(text="👎 не надо", callback_data=f"ev:{pid}:0")],
                              [B(text="🔗 пост", url=link)]])


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
