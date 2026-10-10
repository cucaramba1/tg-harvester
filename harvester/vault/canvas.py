"""Холст Obsidian (JSON Canvas) и раскладка групп и нод."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .fs import atomic_write

NOTE_W, NOTE_H, GAP = 400, 120, 20
PAD_X, PAD_TOP, PAD_BOTTOM = 30, 50, 30
GROUP_W, COL_STEP, ROW_GAP, SIDE_GAP = 460, 500, 120, 200
IDEAS_GROUP = "💡 Идеи"


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def _inside(n: dict, g: dict) -> bool:
    return (n["x"] >= g["x"] and n["y"] >= g["y"]
            and n["x"] + n["width"] <= g["x"] + g["width"] and n["y"] + n["height"] <= g["y"] + g["height"])


class Canvas:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        data.setdefault("nodes", [])
        data.setdefault("edges", [])

    @classmethod
    def load(cls, path: Path) -> "Canvas":
        data = json.loads(path.read_text("utf-8")) if path.exists() else {"nodes": [], "edges": []}
        return cls(path, data)

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    @property
    def nodes(self) -> list[dict]:
        return self.data["nodes"]

    @property
    def edges(self) -> list[dict]:
        return self.data["edges"]

    def node(self, nid: str) -> dict | None:
        return next((n for n in self.nodes if n.get("id") == nid), None)

    def groups(self) -> list[dict]:
        return [n for n in self.nodes if n.get("type") == "group"]

    def group(self, label: str) -> dict | None:
        return next((g for g in self.groups() if g.get("label") == label), None)

    def file_node(self, rel: str) -> dict | None:
        return next((n for n in self.nodes if n.get("type") == "file" and n.get("file") == rel), None)

    def children(self, g: dict) -> list[dict]:
        return [n for n in self.nodes if n is not g and n.get("type") != "group" and _inside(n, g)]

    def bounds(self, skip=frozenset()) -> tuple[int, int, int, int] | None:
        ns = [n for n in self.nodes if n.get("id") not in skip]
        if not ns:
            return None
        return (min(n["x"] for n in ns), min(n["y"] for n in ns),
                max(n["x"] + n["width"] for n in ns), max(n["y"] + n["height"] for n in ns))

    def add_node(self, **fields) -> dict:
        n = {"id": new_id(), **fields}
        self.nodes.append(n)
        return n

    def add_edge(self, from_id: str, to_id: str) -> dict:
        e = {"id": new_id(), "fromNode": from_id, "toNode": to_id}
        self.edges.append(e)
        return e

    def remove_node(self, nid: str) -> None:
        self.data["nodes"] = [n for n in self.nodes if n.get("id") != nid]
        self.data["edges"] = [e for e in self.edges if nid not in (e.get("fromNode"), e.get("toNode"))]

    def remove_edge(self, eid: str) -> None:
        self.data["edges"] = [e for e in self.edges if e.get("id") != eid]

    def move(self, g: dict, x: int, y: int) -> None:
        dx, dy = x - g["x"], y - g["y"]
        for n in [g, *self.children(g)]:
            n["x"] += dx
            n["y"] += dy


def _theme_row_y(cv: Canvas, themes: list[str]) -> int:
    skip: set[str] = set()
    for g in cv.groups():
        if g.get("label") in themes:
            skip.add(g["id"])
            skip.update(c["id"] for c in cv.children(g))
    b = cv.bounds(skip)
    return 0 if b is None else b[3] + ROW_GAP


def ensure_theme_group(cv: Canvas, label: str, themes: list[str]) -> dict:
    """Группа темы в ряду под всем остальным. Старую группу с тем же именем переносим в ряд."""
    row_y = _theme_row_y(cv, themes)
    x = themes.index(label) * COL_STEP
    g = cv.group(label)
    if g is None:
        return cv.add_node(type="group", label=label, x=x, y=row_y, width=GROUP_W, height=PAD_TOP + PAD_BOTTOM)
    if g["y"] < row_y:
        cv.move(g, x, row_y)
    return g


def ensure_side_group(cv: Canvas, label: str = IDEAS_GROUP) -> dict:
    """Группа «💡 Идеи» справа от всего содержимого L2-холста."""
    if g := cv.group(label):
        return g
    b = cv.bounds()
    x, y = (0, 0) if b is None else (b[2] + SIDE_GAP, b[1])
    return cv.add_node(type="group", label=label, x=x, y=y, width=GROUP_W, height=PAD_TOP + PAD_BOTTOM)


def add_to_group(cv: Canvas, g: dict, rel: str) -> dict:
    kids = cv.children(g)
    y = max((k["y"] + k["height"] for k in kids), default=g["y"] + PAD_TOP - GAP) + GAP
    n = cv.add_node(type="file", file=rel, x=g["x"] + PAD_X, y=y, width=NOTE_W, height=NOTE_H)
    g["height"] = max(g["height"], y + NOTE_H + PAD_BOTTOM - g["y"])
    return n


def drop_empty_side_group(cv: Canvas) -> None:
    g = cv.group(IDEAS_GROUP)
    if g and not cv.children(g):
        cv.remove_node(g["id"])
