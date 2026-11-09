"""Аудио -> текст через Groq API (whisper-large-v3-turbo): локальной модели нет, RAM почти не растёт.
Длинное аудио режется на куски по 15 минут (лимит файла free-плана 25 МБ), при лимите Groq (429) ждём retry-after, работу не бросаем."""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import aiohttp

from .config import Settings

log = logging.getLogger(__name__)
_lock = asyncio.Lock()  # бесплатный лимит Groq общий: куски идут строго по одному

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
PROMPT = "Пост из Telegram-канала: нейросети, промпты, бизнес, маркетинг, здоровье, музыка, саморазвитие."
CHUNK_SEC = 15 * 60
PAUSE_SEC = 3          # пауза между кусками, чтобы не выжигать лимит
MAX_ATTEMPTS = 6
MAX_WAIT_SEC = 3700    # лимит free-плана считается по часу: ждём до часа
FFMPEG_TIMEOUT = 600


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT)
    finally:
        if proc.returncode is None:  # таймаут или отмена: не оставляем ffmpeg-сироту
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]}: {err.decode(errors='ignore')[-300:]}")
    return out.decode()


async def _duration(audio: Path) -> float:
    out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio))
    return float(out.strip())


async def _post(s: Settings, chunk: Path) -> str:
    for attempt in range(MAX_ATTEMPTS):
        form = aiohttp.FormData()
        form.add_field("file", chunk.read_bytes(), filename=chunk.name, content_type="audio/ogg")
        for k, v in (("model", s.stt_model), ("language", "ru"), ("temperature", "0"), ("prompt", PROMPT),
                     ("response_format", "json")):
            form.add_field(k, v)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as ses:
                async with ses.post(URL, data=form, headers={"Authorization": f"Bearer {s.groq_key}"}) as r:
                    body = await r.text()
                    if r.status == 200:
                        return str(json.loads(body).get("text") or "").strip()
                    retry = r.status == 429 or r.status >= 500
                    wait = float(r.headers.get("retry-after", 30)) if r.status == 429 else 20
                    log.warning("groq stt http %s (попытка %s): %s", r.status, attempt + 1, body[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            retry, wait = True, 20
            log.warning("groq stt сеть (попытка %s): %s", attempt + 1, e)
        if not retry or attempt == MAX_ATTEMPTS - 1:
            break
        await asyncio.sleep(min(wait, MAX_WAIT_SEC))
    raise RuntimeError("Groq не смог расшифровать аудио")


async def transcribe(s: Settings, audio: Path) -> str:
    if not s.groq_key:
        raise RuntimeError("нет GROQ_API_KEY в .env")
    async with _lock:
        dur = await _duration(audio)
        parts: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            for i, off in enumerate(range(0, max(int(dur), 1), CHUNK_SEC)):
                chunk = Path(tmp) / f"c{i}.ogg"
                await _run("ffmpeg", "-v", "error", "-y", "-ss", str(off), "-t", str(CHUNK_SEC), "-i", str(audio),
                           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(chunk))
                if i:
                    await asyncio.sleep(PAUSE_SEC)
                parts.append(await _post(s, chunk))
        return "\n".join(p for p in parts if p).strip()
