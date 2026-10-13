"""Настройки: .env (секреты и пути) + config/*.yaml + data/calibration.json (пороги после пилота)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

CALIBRATED = ("note_threshold", "section_threshold")


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    backfill: bool = True


@dataclass
class Settings:
    root: Path
    vault: Path
    bot_token: str
    owner_id: int
    api_id: int
    api_hash: str
    claude_bin: str
    whisper_bin: str
    whisper_model: str
    goals_note: str
    channels: list[Channel]
    retro_until: date
    retro_cap: int
    retro_fallback_days: int
    chunk: int
    request_pause: float
    min_chars: int
    ad_markers: list[str]
    max_images: int
    min_free_gb: float
    embed_model: str
    note_threshold: float
    section_threshold: float
    explore_quota: int
    skip_sections: list[str]
    batch_size: int
    max_calls_per_night: int
    retro_multiplier: float
    max_images_per_night: int
    claude_timeout: int
    cards_per_day: int
    cards_hour: int
    snapshot_days: int
    attachments_dir: str
    video_asks_per_day: int = 5
    max_keep_gb: float = 1.0

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "harvester.db"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "harvester"  # Telethon сам допишет .session

    @property
    def keep_dir(self) -> Path:
        return self.data_dir / "keep"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def index_path(self) -> Path:
        return self.data_dir / "vault_index.npz"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def calibration_path(self) -> Path:
        return self.data_dir / "calibration.json"

    def channel_name(self, chat_id: int) -> str:
        return next((c.name for c in self.channels if c.id == chat_id), str(chat_id))


def load_settings(root: Path | None = None) -> Settings:
    root = root or Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    cfg = yaml.safe_load((root / "config" / "settings.yaml").read_text("utf-8"))
    raw_channels = yaml.safe_load((root / "config" / "channels.yaml").read_text("utf-8"))["channels"]
    cal = root / "data" / "calibration.json"
    if cal.exists():
        cfg.update({k: v for k, v in json.loads(cal.read_text("utf-8")).items() if k in CALIBRATED})
    env = lambda k, d="": os.getenv(k, d).strip()  # noqa: E731
    home = Path.home()
    ru = cfg["retro_until"]
    return Settings(
        root=root,
        vault=Path(env("VAULT_DIR", str(home / "Documents/Obsidian storage"))),
        bot_token=env("BOT_TOKEN"),
        owner_id=int(env("OWNER_ID") or 0),
        api_id=int(env("TG_API_ID") or 0),
        api_hash=env("TG_API_HASH"),
        claude_bin=env("CLAUDE_BIN", str(home / ".npm-global/bin/claude")),
        whisper_bin=env("WHISPER_BIN", str(home / ".local/bin/mlx_whisper")),
        whisper_model=env("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"),
        goals_note=cfg["goals_note"],
        channels=[Channel(int(c["id"]), str(c["name"]), bool(c.get("backfill", True))) for c in raw_channels],
        retro_until=ru if isinstance(ru, date) else date.fromisoformat(str(ru)),
        retro_cap=int(cfg["retro_cap"]),
        retro_fallback_days=int(cfg["retro_fallback_days"]),
        chunk=int(cfg["chunk"]),
        request_pause=float(cfg["request_pause"]),
        min_chars=int(cfg["min_chars"]),
        ad_markers=[str(x) for x in cfg["ad_markers"]],
        max_images=int(cfg["max_images"]),
        min_free_gb=float(cfg["min_free_gb"]),
        embed_model=str(cfg["embed_model"]),
        note_threshold=float(cfg["note_threshold"]),
        section_threshold=float(cfg["section_threshold"]),
        explore_quota=int(cfg["explore_quota"]),
        skip_sections=[str(x) for x in cfg.get("skip_sections") or []],
        batch_size=int(cfg["batch_size"]),
        max_calls_per_night=int(cfg["max_calls_per_night"]),
        retro_multiplier=float(cfg["retro_multiplier"]),
        max_images_per_night=int(cfg["max_images_per_night"]),
        claude_timeout=int(cfg["claude_timeout"]),
        cards_per_day=int(cfg["cards_per_day"]),
        cards_hour=int(cfg["cards_hour"]),
        snapshot_days=int(cfg["snapshot_days"]),
        attachments_dir=str(cfg["attachments_dir"]),
        video_asks_per_day=int(cfg.get("video_asks_per_day", 5)),
        max_keep_gb=float(cfg.get("max_keep_gb", 1.0)),
    )
