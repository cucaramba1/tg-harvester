"""Live-процесс: Telethon-сборщик + обогатитель + матчер + бот в одном event loop."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from .bot import App, build_dispatcher
from .collector import Collector, TgClient
from .config import Settings
from .db import DB
from .enricher import Enricher
from .matcher import FastEmbedder, Matcher, VaultVectors
from .writer import Writer

log = logging.getLogger(__name__)


async def _collect(col: Collector) -> None:
    while True:
        try:
            if not await col.step():
                await asyncio.sleep(600)  # всё дочитано, дальше живут события
        except Exception:
            log.exception("collect")
            await asyncio.sleep(60)


async def _process(db: DB, enr: Enricher, mat: Matcher, vectors: VaultVectors) -> None:
    refreshed = None
    while True:
        try:
            if refreshed is None or time.monotonic() - refreshed > 86400:
                await asyncio.to_thread(vectors.refresh)
                refreshed = time.monotonic()
            busy = False
            for p in db.posts("new", 20):
                busy = True
                await enr.process(p)
            for p in db.videos_approved(3):
                busy = True
                await enr.analyze_video(p)
            for p in db.posts("enriched", 50):
                busy = True
                await asyncio.to_thread(mat.process, p)
            if not busy:
                await asyncio.sleep(10)
        except Exception:
            log.exception("process")
            await asyncio.sleep(60)


async def _tick(app: App, bot) -> None:
    while True:
        await app.tick(bot)
        await asyncio.sleep(60)


async def run_live(s: Settings) -> None:
    from aiogram import Bot
    from telethon import TelegramClient, events

    db = DB(s.db_path)
    bot = Bot(s.bot_token)
    client = TelegramClient(str(s.session_path), s.api_id, s.api_hash)
    client.flood_sleep_threshold = 24 * 3600  # FloodWait любой длины просто пережидаем
    await client.connect()
    if not await client.is_user_authorized():
        today = date.today().isoformat()
        if db.kv_get("login_alert") != today:
            db.kv_set("login_alert", today)
            await bot.send_message(s.owner_id, "🔑 Сессия Telegram не авторизована. В терминале: "
                                               "cd ~/Claude\\ Code/tg-harvester && uv run python -m harvester login")
        await bot.session.close()
        await asyncio.sleep(3600)  # иначе launchd перезапустит нас через 30 секунд
        return
    await client.get_dialogs()  # кэш сущностей каналов для новой сессии
    tg = TgClient(client)
    col = Collector(s, db, tg)
    await col.init()
    client.add_event_handler(col.on_new, events.NewMessage(chats=[c.id for c in s.channels]))
    vectors = VaultVectors(s, FastEmbedder(s.embed_model, s.models_dir))
    app = App(s, db, Writer(s, db))
    dp = build_dispatcher(app)
    log.info("TG Harvester запущен")
    await asyncio.gather(_collect(col), _process(db, Enricher(s, db, tg), Matcher(s, db, vectors), vectors),
                         _tick(app, bot), dp.start_polling(bot, handle_signals=False))
