"""Общие хелперы тестов: настройки на tmp_path, черновики, фейковый Claude."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from harvester.config import Channel, Settings

CHAT_A = -1001234
CHAT_B = -1005678


def make_settings(tmp: Path, **over) -> Settings:
    vault = tmp / "vault"
    vault.mkdir(exist_ok=True)
    base = dict(
        root=tmp, vault=vault, bot_token="t", owner_id=1, api_id=1, api_hash="h",
        claude_bin="claude", whisper_bin="whisper", whisper_model="m", goals_note="goals.md",
        channels=[Channel(CHAT_A, "Канал А"), Channel(CHAT_B, "Канал Б", backfill=False)],
        retro_until=date(2025, 10, 10), retro_cap=5000, retro_fallback_days=60, chunk=3, request_pause=0,
        min_chars=20, ad_markers=["erid", "розыгрыш"], max_images=4, min_free_gb=10, embed_model="m",
        note_threshold=0.6, section_threshold=0.45, explore_quota=2, skip_sections=["Скиллы на машине"],
        batch_size=2, max_calls_per_night=3, retro_multiplier=2.0, max_images_per_night=1, claude_timeout=10,
        cards_per_day=2, cards_hour=9, snapshot_days=30, attachments_dir="Вложения/Telegram",
    )
    base.update(over)
    return Settings(**base)


def make_draft(db, msg_id: int, mode: str, target: str, *, content: str = "суть поста", title=None,
               keep_image: bool = False, top3=(), images=()) -> int:
    pid = db.add_post(CHAT_A, msg_id, None, "2026-10-01T10:00:00+00:00", "текст поста", None)
    db.update_post(pid, status="drafted", top3=[list(t) for t in top3], images=list(images))
    return db.add_draft(pid, mode, target, title, content, keep_image, [])


class FakeClaude:
    """Отдаёт заготовленные ответы по очереди. Ответ: dict, callable(prompt) или исключение."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, s, prompt, **kw):
        self.calls.append((prompt, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(prompt) if callable(r) else r
