"""Точка входа: python -m harvester <команда>."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .config import load_settings


def _logging(s, cmd: str) -> None:
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(s.logs_dir / f"harvester-{cmd}.log", encoding="utf-8"),
                                  logging.StreamHandler()])


async def _login(s) -> None:
    from telethon import TelegramClient
    s.data_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(s.session_path), s.api_id, s.api_hash)
    # phone= вместо дефолта: иначе Telethon примет вставленный токен бота за телефон и войдёт ботом
    await client.start(phone=lambda: input("Номер телефона аккаунта (+7...): "))  # дальше код и пароль 2FA
    me = await client.get_me()
    if me.bot:
        print("❌ Вошёл как бот, нужен личный аккаунт. Удали data/harvester.session и повтори.")
        await client.disconnect()
        return
    print(f"✅ Вошёл как {me.first_name}. Сессия: {s.session_path}.session")
    await client.disconnect()


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    p = argparse.ArgumentParser(prog="harvester")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("login", "live", "bot", "night", "audit", "calibrate"):
        sub.add_parser(name)
    av = sub.add_parser("autovideo")
    av.add_argument("action", choices=["backfill", "report"])
    sec = sub.add_parser("section")
    sec.add_argument("name")
    a = p.parse_args(argv)
    s = load_settings(root)
    _logging(s, a.cmd)

    if a.cmd == "section":
        from .vault_setup import ensure_section
        created = ensure_section(s.vault, a.name)
        print("Создано: " + ", ".join(created) if created else f"Раздел «{a.name}» уже есть")
    elif a.cmd == "autovideo":
        from .autovideo import report_text
        from .db import DB
        db = DB(s.db_path)
        if a.action == "backfill":
            print(f"Добавлено меток из video_state: {db.backfill_video_labels()}")
        else:
            print(report_text(s, db))
    elif a.cmd == "login":
        asyncio.run(_login(s))
    elif a.cmd == "live":
        from .live import run_live
        asyncio.run(run_live(s))
    elif a.cmd == "bot":
        from .live import run_bot
        asyncio.run(run_bot(s))
    elif a.cmd == "night":
        from .brain import run_night
        from .db import DB
        from .files import prune_snapshots
        rep = asyncio.run(run_night(s, DB(s.db_path)))
        prune_snapshots(s)
        print(rep)
    elif a.cmd == "audit":
        from .auditor import run_audit
        from .db import DB
        print(asyncio.run(run_audit(s, DB(s.db_path))))
    elif a.cmd == "calibrate":
        from .db import DB
        from .matcher import calibrate
        res = calibrate(s, DB(s.db_path))
        print(f"Новые пороги: {res}" if res else "Мало разметки: нужен хотя бы один 👍 и один 👎")
    return 0
