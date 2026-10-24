"""Live-процесс: Telethon-сборщик + обогатитель + матчер + бот в одном event loop."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from datetime import date

from .bot import App, build_dispatcher
from .collector import Collector, TgClient
from .config import Settings
from .db import DB
from .enricher import Enricher
from .topic_check import run_topic_checks
from .topics import fetch_meta, split_big_topics, tag_topics
from .writer import Writer

log = logging.getLogger(__name__)
MATCH_EVERY = 3600  # постов ~3 в час, разбор черновиков ночью: матчить чаще незачем


async def _collect(col: Collector) -> None:
    while True:
        try:
            if not await col.step():
                await asyncio.sleep(600)  # всё дочитано, дальше живут события
        except Exception:
            log.exception("collect")
            await asyncio.sleep(60)


async def _run_match() -> None:
    """Матчинг отдельным процессом (`harvester match`): модель не висит в памяти live между запусками."""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "harvester", "match",
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        code = await asyncio.wait_for(proc.wait(), 1800)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.error("harvester match висел больше 30 минут, убит")
        return
    if code:
        log.error("harvester match завершился с кодом %s, см. logs/harvester-match.log", proc.returncode)


async def _process(db: DB, enr: Enricher) -> None:
    last_match = None
    while True:
        try:
            busy = False
            for p in db.posts("new", 20):
                busy = True
                await enr.process(p)
            for p in db.videos_approved(3):
                busy = True
                await enr.analyze_video(p)
            if db.posts("enriched", 1) and (last_match is None or time.monotonic() - last_match > MATCH_EVERY):
                last_match = time.monotonic()
                await _run_match()
            if not busy:
                await asyncio.sleep(10)
        except Exception:
            log.exception("process")
            await asyncio.sleep(60)


async def _topics_prep(s: Settings, db: DB, tg) -> None:
    """Метаданные файла и темы для очереди видео-вопросов. Всё через единственную Telethon-сессию live-процесса."""
    from .brain import run_router
    while True:
        try:
            meta = await fetch_meta(tg, db, 100)
            tagged = await tag_topics(s, db, run_router, 80) if s.video_by_topic else 0
            if s.video_by_topic and not tagged and not db.queue_without_topic(1):
                tagged = await split_big_topics(s, db, run_router, 80)  # всё размечено: дробим слишком широкие темы
            stuck = s.video_by_topic and not tagged and (bool(db.queue_without_topic(1)) or any(
                r["n"] > s.topic_max_size and db.kv_get(f"split_giveup:{r['topic']}") != "1" for r in db.topic_overview()))
            # лимиты бесплатных моделей плавающие: после неудачи ждём подольше, ночному разбору роутер тоже нужен
            await asyncio.sleep(600 if stuck else 20 if (meta or tagged) else 900)
        except Exception:
            log.exception("topics_prep")
            await asyncio.sleep(120)


async def _topic_checks(s: Settings, db: DB, tg) -> None:
    """Ночная проверка тем, которые Некит отметил кнопкой. Окно и бюджет внутри run_topic_checks."""
    from .brain import run_router
    while True:
        try:
            if s.video_by_topic:
                await run_topic_checks(s, db, tg, run_router)
        except Exception:
            log.exception("topic_checks")
        await asyncio.sleep(600)


async def _tick(app: App, bot) -> None:
    while True:
        await app.tick(bot)
        await asyncio.sleep(60)


async def _rss_guard() -> None:
    """Потолок памяти: при превышении процесс выходит, launchd (KeepAlive) поднимает его заново. Каждые 10 минут пишет RSS в лог."""
    limit = int(os.environ.get("HARVESTER_RSS_MAX_MB", "700"))
    n = 0
    while True:
        await asyncio.sleep(60)
        try:
            out = await asyncio.to_thread(subprocess.check_output, ["ps", "-o", "rss=", "-p", str(os.getpid())])
            mb = int(out) // 1024
        except Exception:
            log.exception("rss_guard")
            continue
        n += 1
        if n % 10 == 0:
            log.info("RSS %d МБ (потолок %d)", mb, limit)
        if mb > limit:
            log.error("RSS %d МБ выше потолка %d МБ, выхожу для перезапуска", mb, limit)
            logging.shutdown()
            os._exit(75)


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
    app = App(s, db, Writer(s, db))
    dp = build_dispatcher(app)
    log.info("TG Harvester запущен")
    await asyncio.gather(_collect(col), _process(db, Enricher(s, db, tg)),
                         _topics_prep(s, db, tg), _topic_checks(s, db, tg),
                         _tick(app, bot), _rss_guard(), dp.start_polling(bot, handle_signals=False),
                         _exit_on_disconnect(client))


async def _exit_on_disconnect(client) -> None:
    """Telethon исчерпал переподключения: без выхода live жил зомби и ничего не собирал. launchd (KeepAlive) поднимет заново,
    а init() доберёт пропущенное."""
    await client.disconnected
    log.error("Telethon отключился насовсем, выхожу для перезапуска")
    logging.shutdown()
    os._exit(75)


async def run_bot(s: Settings) -> None:
    """Только бот ревью: очередь карточек и видео-вопросов из базы. Без Telethon, моделей, сбора и обработки."""
    from aiogram import Bot

    db = DB(s.db_path)
    bot = Bot(s.bot_token)
    app = App(s, db, Writer(s, db))
    dp = build_dispatcher(app)
    log.info("TG Harvester: режим «только бот»")
    await asyncio.gather(_tick(app, bot), dp.start_polling(bot, handle_signals=False))
