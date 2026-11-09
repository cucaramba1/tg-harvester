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
    for name in ("login", "live", "bot", "night", "audit", "calibrate", "match", "dedup", "applier"):
        sub.add_parser(name)
    av = sub.add_parser("autovideo")
    av.add_argument("action", choices=["backfill", "report"])
    tp = sub.add_parser("topics")
    tp.add_argument("action", choices=["report"])
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
    elif a.cmd == "topics":
        from .db import DB
        db = DB(s.db_path)
        ov = db.topic_overview()
        print(f"Тем в очереди: {len(ov)}, вопросов с темой: {sum(r['n'] for r in ov)}, без темы: {len(db.queue_without_topic(10**6))}")
        for r in ov[:40]:
            print(f"  {r['topic']}: {r['n']} видео, разных файлов {r['files']}")
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
    elif a.cmd == "applier":
        from .applier import run_applier
        from .db import DB
        print(run_applier(s, DB(s.db_path)))
    elif a.cmd == "audit":
        from .auditor import run_audit
        from .db import DB
        print(asyncio.run(run_audit(s, DB(s.db_path))))
    elif a.cmd == "match":
        # Модель весит ~700 МБ и внутри процесса до конца не выгружается, поэтому матчинг живёт в коротком процессе:
        # live запускает его раз в час, память возвращается системе при выходе.
        from .db import DB
        from .matcher import FastEmbedder, Matcher, VaultVectors
        db = DB(s.db_path)
        vectors = VaultVectors(s, FastEmbedder(s.embed_model, s.models_dir))
        vectors.refresh()
        mat, n = Matcher(s, db, vectors), 0
        for p in db.posts("enriched", 10**6):
            try:
                mat.process(p)
                n += 1
            except Exception:
                logging.exception("match %s", p["id"])
        print(f"Сопоставлено постов: {n}")
    elif a.cmd == "dedup":
        # Разово по накопленной очереди (новые посты проверяет match). Модель живёт только в этом процессе.
        from .db import DB
        from .matcher import DUP_MIN_CHARS, DupIndex, FastEmbedder, mark_duplicate
        db = DB(s.db_path)
        emb = FastEmbedder(s.embed_model, s.models_dir)
        idx = DupIndex(s.dup_threshold, db.dup_pool())
        known = set(idx.ids)
        # сначала уже разобранное (оригиналы), потом очередь в порядке ночи: дублем становится тот, что ниже
        rows = db.c.execute("SELECT * FROM posts WHERE emb IS NULL AND status IN ('drafted','written')").fetchall()
        rows += [r for r in db.night_queue(0) if r["status"] == "candidate"]
        body = lambda r: "\n".join(x for x in (r["text"], r["extra_text"]) if x).strip()
        rows = [r for r in rows if r["id"] not in known and len(body(r)) >= DUP_MIN_CHARS]
        dup = 0
        for i in range(0, len(rows), 64):
            part = rows[i:i + 64]
            for r, v in zip(part, emb.embed([body(r)[:2000] for r in part])):
                of = idx.find(v) if r["status"] == "candidate" else None
                if of is not None:
                    mark_duplicate(s, db, r["id"], of)
                    dup += 1
                else:
                    db.update_post(r["id"], emb=v.astype("float32").tobytes())
                    idx.add(r["id"], v)
        print(f"Проверено {len(rows)}, дублей {dup}")
    elif a.cmd == "calibrate":
        from .db import DB
        from .matcher import calibrate
        res = calibrate(s, DB(s.db_path))
        print(f"Новые пороги: {res}" if res else "Мало разметки: нужен хотя бы один 👍 и один 👎")
    return 0
