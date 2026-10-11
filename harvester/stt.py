"""Голос -> текст через локальный mlx_whisper: модель живёт только на время расшифровки."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)
_lock = asyncio.Lock()  # две модели параллельно на 8 ГБ не запускаем

PROMPT = "Пост из Telegram-канала: нейросети, промпты, бизнес, маркетинг, здоровье, музыка, саморазвитие."


async def transcribe(s: Settings, audio: Path) -> str:
    async with _lock:
        with tempfile.TemporaryDirectory() as out:
            proc = await asyncio.create_subprocess_exec(
                s.whisper_bin, str(audio), "--model", s.whisper_model, "--language", "ru",
                "--output-format", "txt", "--output-dir", out, "--verbose", "False",
                "--condition-on-previous-text", "False", "--initial-prompt", PROMPT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")},
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("Whisper не успел за 10 минут")
            if proc.returncode != 0:
                log.error("whisper failed: %s", err.decode(errors="ignore")[-500:])
                raise RuntimeError("Whisper не смог расшифровать аудио")
            txt = next(Path(out).glob("*.txt"), None)
            return txt.read_text(encoding="utf-8").strip() if txt else ""
