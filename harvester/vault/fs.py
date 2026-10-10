"""Атомарная запись и wikilinks."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Пишем во временный файл рядом и подменяем: Obsidian никогда не увидит полфайла."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".harvester-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def wikilink(rel: str) -> str:
    """Путь от корня vault -> [[путь|имя]]. Путь нужен, потому что имена бывают не уникальны."""
    target = rel[:-3] if rel.endswith(".md") else rel
    name = target.rsplit("/", 1)[-1]
    return f"[[{target}|{name}]]" if "/" in target else f"[[{target}]]"
