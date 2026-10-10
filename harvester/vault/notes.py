"""Имена файлов заметок и пункты MOC."""
from __future__ import annotations

import re
from pathlib import Path

from .fs import atomic_write

_BAD = re.compile(r'[\\*?"<>|#^\[\]]')


def safe_filename(title: str) -> str:
    name = title.replace(":", " -").replace("/", "-")
    name = _BAD.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    return name[:100].rstrip() or "Заметка"


def unique_note_path(folder: Path, title: str) -> Path:
    base = safe_filename(title)
    path, n = folder / f"{base}.md", 2
    while path.exists():
        path, n = folder / f"{base} ({n}).md", n + 1
    return path


def moc_add(moc: Path, theme: str, item: str) -> None:
    text = moc.read_text("utf-8") if moc.exists() else f"# {moc.stem}\n"
    lines = text.splitlines()
    head = f"## {theme}"
    hi = next((i for i, line in enumerate(lines) if line.strip() == head), None)
    if hi is None:
        atomic_write(moc, text.rstrip("\n") + f"\n\n{head}\n\n- {item}\n")
        return
    end = hi + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    last = end
    while last > hi + 1 and not lines[last - 1].strip():
        last -= 1
    lines.insert(last, f"- {item}")
    atomic_write(moc, "\n".join(lines) + "\n")


def moc_remove(moc: Path, item: str) -> None:
    """Убирает пункт. Если в его разделе больше ничего нет, убирает и заголовок раздела."""
    if not moc.exists():
        return
    out = []
    for sec in re.split(r"(?m)^(?=## )", moc.read_text("utf-8")):
        lines = sec.splitlines(keepends=True)
        kept = [x for x in lines if x.strip() != f"- {item}"]
        if len(kept) != len(lines) and sec.startswith("## ") and not any(x.strip() for x in kept[1:]):
            continue
        out.append("".join(kept))
    atomic_write(moc, "".join(out).rstrip("\n") + "\n")
