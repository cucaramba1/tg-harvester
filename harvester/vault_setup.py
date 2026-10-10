"""Новый раздел L2: папка, MOC, холст и file-нода в «Обо мне» (корень L1)."""
from __future__ import annotations

from pathlib import Path

from .vault.canvas import Canvas
from .vault.fs import atomic_write

ROOT_CANVAS = "Обо мне.canvas"
ROOT_GROUP = "Сферы жизни (2-й уровень)"
COLS, CELL_W, CELL_H, STEP_X, STEP_Y, PAD_X, PAD_TOP, PAD_BOTTOM = 5, 440, 180, 460, 200, 30, 60, 40


def ensure_section(vault: Path, name: str) -> list[str]:
    created: list[str] = []
    moc_rel = f"{name}/{name} — карта заметок.md"
    moc = vault / moc_rel
    if not moc.exists():
        atomic_write(moc, f"# {name} — карта заметок\n")
        created.append(moc_rel)
    cv_path = vault / f"{name}.canvas"
    if not cv_path.exists():
        cv = Canvas.load(cv_path)
        cv.add_node(type="file", file=moc_rel, x=0, y=0, width=400, height=120)
        cv.save()
        created.append(cv_path.name)
    root = vault / ROOT_CANVAS
    if root.exists():
        cv = Canvas.load(root)
        g = cv.group(ROOT_GROUP)
        if g is not None and cv.file_node(f"{name}.canvas") is None:
            i = len(cv.children(g))
            x = g["x"] + PAD_X + (i % COLS) * STEP_X
            y = g["y"] + PAD_TOP + (i // COLS) * STEP_Y
            cv.add_node(type="file", file=f"{name}.canvas", x=x, y=y, width=CELL_W, height=CELL_H)
            g["height"] = max(g["height"], y + CELL_H + PAD_BOTTOM - g["y"])
            cv.save()
            created.append(ROOT_CANVAS)
    return created
