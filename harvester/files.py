"""Файловые мелочи: снапшоты, временные картинки постов, разделы vault, MOC."""
from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

from .config import Settings


def snapshot(s: Settings, path: Path) -> Path | None:
    """Копия файла до первой правки за день: data/snapshots/<дата>/<путь в vault>."""
    if not path.exists():
        return None
    dest = s.snapshots_dir / date.today().isoformat() / path.relative_to(s.vault)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    return dest


def prune_snapshots(s: Settings, today: date | None = None) -> int:
    edge = (today or date.today()) - timedelta(days=s.snapshot_days)
    n = 0
    if s.snapshots_dir.exists():
        for d in s.snapshots_dir.iterdir():
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue
            if day < edge:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n


def drop_keep(s: Settings, post_id: int) -> None:
    shutil.rmtree(s.keep_dir / str(post_id), ignore_errors=True)


def list_sections(vault: Path) -> list[str]:
    """Разделы L2: папка в корне vault, рядом с которой лежит одноимённый холст."""
    return sorted(d.name for d in vault.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and (vault / f"{d.name}.canvas").exists())


def find_moc(vault: Path, section: str) -> Path | None:
    name = f"{section} — карта заметок.md"
    return next((p for p in (vault / section / name, vault / name) if p.exists()), None)
