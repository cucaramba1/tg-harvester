# TG Harvester Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сервис на маке собирает посты из 24 Telegram-каналов, бесплатно фильтрует их локально, ночью разбирает лучшие через `claude -p` в пределах лимита и после одобрения Некита в боте дописывает или создаёт заметки в Obsidian.

**Architecture:** Три launchd-процесса делят одну SQLite (WAL): `live` (Telethon-сборщик + обогатитель + матчер на эмбеддингах + aiogram-бот), `night` (батчи в `claude -p`), `audit` (проверка записанного за неделю). Модули общаются только через статус поста в SQLite. Код работы с vault скопирован из idea-bot.

**Tech Stack:** Python 3.12+, uv, Telethon, aiogram 3, fastembed (ONNX, без torch), numpy, ocrmac (macOS Vision), mlx_whisper, ffmpeg, pytest.

**Spec:** `docs/specs/2026-10-10-tg-harvester-design.md`

## Global Constraints

- Проект: `~/Claude Code/tg-harvester`, пакет `harvester/`, тесты `tests/`, запуск тестов `uv run pytest -q`.
- Python `>=3.12`, зависимости только через `uv add`.
- Тесты никогда не трогают живой vault (`~/Documents/Obsidian storage`): только `tmp_path`. Живой vault трогаем только в задаче 11 вместе с Некитом.
- В Telegram ничего не отмечается прочитанным. Никаких отправок сообщений людям: бот пишет только `OWNER_ID` (сам Некит).
- Существующий текст заметки выше `## Из Telegram` не меняется ни на байт.
- В текстах для людей (бот, заметки, README) нет длинного тире `—`. Исключение: имена MOC-файлов по конвенции vault `<Раздел> — карта заметок.md`.
- Если на диске меньше `min_free_gb` (10) GB, медиа не скачиваются.
- Коммит после каждой задачи: `feat(harvester): <что>` + пустая строка + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Секреты только в `.env` (не в git).

## Review Focus

1. Некит поправил заметку руками между записью и откатом: откат не должен портить файл (sha-проверка, поиск блока по содержимому, иначе «changed»). Тесты в задаче 2.
2. Части альбома приходят в любом порядке, подпись в не первой части: один пост с подписью, без дублей после рестарта. Тесты в задачах 1 и 4.
3. Канал с отметкой прочитанного 0 (никогда не открывал): фаза «новое» не должна ползти по годам истории. Тест в задаче 4.
4. Claude вернул несуществующую заметку или путь `../`: никакой записи вне vault, откат в раздел или skip. Тесты в задачах 2 и 7.
5. Процесс упал или мак уснул посреди чанка: продолжение с курсора без дублей. Тест в задаче 4.

## Структура файлов

```
tg-harvester/
├─ pyproject.toml, .gitignore, .env.example, README.md
├─ config/settings.yaml      параметры
├─ config/channels.yaml      каналы (порядок = порядок обхода)
├─ harvester/
│  ├─ __main__.py            → cli.main()
│  ├─ cli.py                 login | live | night | audit | calibrate | section
│  ├─ config.py              Settings, Channel, load_settings
│  ├─ db.py                  DB: posts, channels, drafts, blocks, labels, audits, alerts, kv
│  ├─ files.py               snapshot, prune_snapshots, drop_keep, list_sections, find_moc
│  ├─ vault/fs.py            atomic_write, wikilink (копия idea-bot)
│  ├─ vault/canvas.py        Canvas и раскладка (копия idea-bot)
│  ├─ vault/notes.py         safe_filename, unique_note_path, moc_add, moc_remove
│  ├─ vault_setup.py         ensure_section (раздел L2 + связь с «Обо мне»)
│  ├─ writer.py              Writer.apply / rollback
│  ├─ rules.py               ad_reason
│  ├─ collector.py           media_kind, TgClient, Collector
│  ├─ stt.py                 transcribe (mlx_whisper)
│  ├─ enricher.py            ocr_image, extract_video, free_gb, Enricher
│  ├─ matcher.py             FastEmbedder, VaultVectors, Matcher, usefulness, calibrate
│  ├─ brain.py               run_claude, validate, run_night
│  ├─ auditor.py             run_audit
│  ├─ cards.py               тексты и клавиатуры бота
│  ├─ bot.py                 App (логика кнопок), build_dispatcher
│  └─ live.py                run_live (сборка live-процесса)
├─ scripts/setup.sh          launchd: live, night (03:00), audit (вс 04:00)
└─ tests/ (helpers.py, conftest.py, test_*.py)
```

Статусы поста: `new → enriched → (explore | video_ask | candidate) → drafted → written`, а также `filtered`, `rejected`, `error`. Черновик: `drafted → sent → written | rejected | rolled_back`.

## Пачки

| Чат | Задачи |
|---|---|
| 1 | 1-3: каркас/конфиг/БД, vault + Writer, раздел «Музыка» |
| 2 | 4-6: сборщик, обогатитель, матчер |
| 3 | 7-10: мозг, бот, аудит, CLI + launchd |
| 4 | 11: живой прогон с Некитом (пилот, калибровка) |

---

### Task 1: Каркас, конфиг, БД

**Files:**
- Create: `pyproject.toml` (через `uv init`/`uv add`), `.gitignore`, `.env.example`, `config/settings.yaml`, `config/channels.yaml`, `harvester/__init__.py`, `harvester/config.py`, `harvester/db.py`
- Test: `tests/__init__.py`, `tests/helpers.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_db.py`

**Interfaces:**
- Produces:
  - `Channel(id: int, name: str, backfill: bool = True)`
  - `Settings` (поля ниже), свойства `data_dir, db_path, session_path, keep_dir, tmp_dir, snapshots_dir, logs_dir, index_path, models_dir, calibration_path`, метод `channel_name(chat_id) -> str`
  - `load_settings(root: Path | None = None) -> Settings`
  - `DB(path)` с методами из кода ниже; JSON-поля `media, images, top3, alts, extra` сериализуются автоматически.
  - `tests/helpers.py`: `make_settings(tmp, **over)`, `CHAT_A=-1001234`, `CHAT_B=-1005678`, `make_draft(...)`, `FakeClaude`

- [x] **Step 1: Каркас и зависимости**

```bash
cd "/Users/cucaramba/Claude Code/tg-harvester"
uv init --bare --python 3.12
uv add aiogram telethon python-dotenv pyyaml numpy fastembed ocrmac
uv add --dev pytest
mkdir -p harvester/vault config scripts tests
touch harvester/__init__.py harvester/vault/__init__.py tests/__init__.py
```

Допиши в `pyproject.toml`:

```toml
[tool.uv]
package = false

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

`.gitignore`:

```
.env
.venv/
data/
logs/
__pycache__/
*.pyc
```

`.env.example`:

```
BOT_TOKEN=
OWNER_ID=298824833
TG_API_ID=
TG_API_HASH=
VAULT_DIR=/Users/cucaramba/Documents/Obsidian storage
CLAUDE_BIN=/Users/cucaramba/.npm-global/bin/claude
WHISPER_BIN=/Users/cucaramba/.local/bin/mlx_whisper
WHISPER_MODEL=mlx-community/whisper-large-v3-turbo
```

`config/settings.yaml`:

```yaml
goals_note: Продуктивность и привычки/Мои цели.md
# сбор
retro_until: 2025-10-10
retro_cap: 5000            # больше постов в ретро-окне канала: режем до retro_fallback_days
retro_fallback_days: 60
chunk: 100                 # сообщений за один шаг сборщика
request_pause: 2           # секунд между шагами
# фильтр
min_chars: 80
ad_markers: ["erid", "#реклама", "на правах рекламы", "розыгрыш", "промокод", "#ad"]
max_images: 4
min_free_gb: 10
embed_model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
note_threshold: 0.6        # перекрывается data/calibration.json
section_threshold: 0.45
explore_quota: 10          # постов вне тем в ночь
skip_sections: ["Скиллы на машине"]
# ночь
batch_size: 20
max_calls_per_night: 5     # калибруется на пилоте
retro_multiplier: 2.5      # пока какой-то канал не в фазе done
max_images_per_night: 10
claude_timeout: 300
# бот и запись
cards_per_day: 30
cards_hour: 9
snapshot_days: 30
attachments_dir: Вложения/Telegram
```

`config/channels.yaml`:

```yaml
# Порядок = порядок обхода. backfill: false = только новые посты, без ретро.
channels:
  - {id: -1001009232144, name: "Двач", backfill: false}
  - {id: -1002025880809, name: "Твой пет проект"}
  - {id: -1001104344271, name: "Не баг, а фича"}
  - {id: -1001758234692, name: "XOR"}
  - {id: -1001497050564, name: "Наше тело"}
  - {id: -1002230784863, name: "Мужской доктор"}
  - {id: -1001155004784, name: "Скорая Помощь"}
  - {id: -1001369365024, name: "CMD"}
  - {id: -1001307778786, name: "Эксплойт"}
  - {id: -1001476939504, name: "Эрудиция"}
  - {id: -1001795938797, name: "ТАЙМ-МЕНЕДЖМЕНТ, САМОРАЗВИТИЕ, СИСТЕМНОСТЬ"}
  - {id: -1002824980256, name: "АПОЛЛО"}
  - {id: -1001983260268, name: "Chad Protocol"}
  - {id: -1001167188175, name: "Книги на миллион"}
  - {id: -1001338162138, name: "Кладовка Pavlu"}
  - {id: -1001747738453, name: "Миллион на музыке"}
  - {id: -1001208691137, name: "Халява & Схемы"}
  - {id: -1001438683880, name: "Clean Code"}
  - {id: -1001114591086, name: "Machinelearning"}
  - {id: -1002267212057, name: "AI Sapiens"}
  - {id: -1002296496807, name: "Комната Новой Реальности"}
  - {id: -1001346439625, name: "Not Boring Tech"}
  - {id: -1001758908387, name: "Рома Райт"}
  - {id: -1001774404107, name: "НейроGod Промты / Claude prompt"}
```

- [x] **Step 2: Тестовые хелперы**

`tests/helpers.py`:

```python
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
```

`tests/conftest.py`:

```python
import pytest

from harvester.db import DB
from tests.helpers import make_settings


@pytest.fixture
def s(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def db(s):
    return DB(s.db_path)
```

- [x] **Step 3: Падающие тесты конфига и БД**

`tests/test_config.py`:

```python
import json
import shutil
from datetime import date
from pathlib import Path

from harvester.config import load_settings

REPO = Path(__file__).resolve().parent.parent


def _root(tmp_path, monkeypatch):
    shutil.copytree(REPO / "config", tmp_path / "config")
    for k, v in {"BOT_TOKEN": "x", "OWNER_ID": "42", "TG_API_ID": "7", "TG_API_HASH": "h",
                 "VAULT_DIR": str(tmp_path / "v")}.items():
        monkeypatch.setenv(k, v)
    return tmp_path


def test_load_settings_reads_repo_config(tmp_path, monkeypatch):
    s = load_settings(_root(tmp_path, monkeypatch))
    assert (s.owner_id, s.api_id, s.api_hash) == (42, 7, "h")
    assert len(s.channels) == 24
    assert s.channels[0].name == "Двач" and s.channels[0].backfill is False
    assert all(c.backfill for c in s.channels[1:])
    assert s.retro_until == date(2025, 10, 10)
    assert s.channel_name(-1001758908387) == "Рома Райт"
    assert s.channel_name(-1) == "-1"
    assert s.session_path == tmp_path / "data" / "harvester"


def test_calibration_overrides_thresholds(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    (root / "data").mkdir()
    (root / "data" / "calibration.json").write_text(json.dumps({"note_threshold": 0.71, "junk": 1}), "utf-8")
    s = load_settings(root)
    assert s.note_threshold == 0.71 and s.section_threshold == 0.45
```

`tests/test_db.py`:

```python
import json

from tests.helpers import CHAT_A


def test_add_post_is_idempotent(db):
    assert db.add_post(CHAT_A, 5, None, "2026-10-01T00:00:00+00:00", "текст", None) is not None
    assert db.add_post(CHAT_A, 5, None, "2026-10-01T00:00:00+00:00", "текст", None) is None
    assert db.counts() == {"new": 1}


def test_album_parts_merge_into_one_post_and_keep_text(db):
    pid = db.add_post(CHAT_A, 12, 777, "d", "", "photo")
    assert db.add_post(CHAT_A, 11, 777, "d", "подпись альбома", "photo") is None
    assert db.add_post(CHAT_A, 11, 777, "d", "подпись альбома", "photo") is None  # повтор после рестарта
    assert db.add_post(CHAT_A, 12, 777, "d", "", "photo") is None
    p = db.get_post(pid)
    assert p["text"] == "подпись альбома"
    assert [m["msg_id"] for m in json.loads(p["media"])] == [12, 11]
    assert db.counts() == {"new": 1}


def test_update_post_serializes_json(db):
    pid = db.add_post(CHAT_A, 1, None, "d", "t", None)
    db.update_post(pid, status="enriched", images=["a.jpg"], top3=[["x.md", 0.5]])
    p = db.get_post(pid)
    assert p["status"] == "enriched"
    assert json.loads(p["images"]) == ["a.jpg"] and json.loads(p["top3"]) == [["x.md", 0.5]]


def test_bump_attempts(db):
    pid = db.add_post(CHAT_A, 1, None, "d", "t", None)
    assert db.bump_attempts(pid) == 1 and db.bump_attempts(pid) == 2


def test_channel_upsert_and_retro_active(db):
    assert db.retro_active() is False
    db.save_channel(CHAT_A, read_mark=5, new_cursor=5, retro_cursor=6, retro_until=None, phase="new")
    db.save_channel(CHAT_A, phase="retro")
    r = db.channel(CHAT_A)
    assert r["phase"] == "retro" and r["new_cursor"] == 5
    assert db.retro_active() is True
    db.save_channel(CHAT_A, phase="done")
    assert db.retro_active() is False


def test_shift_blocks_moves_only_blocks_below_in_same_file(db):
    a = db.add_block(1, "n.md", "append", 10, 20, "x", {})
    b = db.add_block(2, "n.md", "append", 30, 40, "y", {})
    c = db.add_block(3, "o.md", "append", 30, 40, "z", {})
    db.shift_blocks("n.md", 25, 5)
    assert [db.get_block(i)["start"] for i in (a, b, c)] == [10, 35, 30]
    assert db.get_block(b)["stop"] == 45


def test_alerts_pop_once_and_kv(db):
    db.push_alert("a")
    assert db.pop_alerts() == ["a"] and db.pop_alerts() == []
    assert db.kv_get("k", "d") == "d"
    db.kv_set("k", "v")
    assert db.kv_get("k") == "v"
```

- [x] **Step 4: Убедиться, что падают**

Run: `uv run pytest -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.config'`

- [x] **Step 5: `harvester/config.py`**

```python
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
    )
```

- [x] **Step 6: `harvester/db.py`**

```python
"""SQLite: посты, каналы, черновики, блоки, разметка, аудит, алерты, kv.

WAL и busy_timeout: live-процесс и ночной процесс пишут в одну базу."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  msg_id INTEGER NOT NULL,
  grouped_id INTEGER,
  date TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  media TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT 'history',
  status TEXT NOT NULL DEFAULT 'new',
  reason TEXT,
  extra_text TEXT NOT NULL DEFAULT '',
  images TEXT NOT NULL DEFAULT '[]',
  score REAL,
  section_score REAL,
  mode TEXT,
  target TEXT,
  top3 TEXT NOT NULL DEFAULT '[]',
  usefulness REAL,
  video_state TEXT NOT NULL DEFAULT 'none',
  attempts INTEGER NOT NULL DEFAULT 0,
  UNIQUE(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS posts_group ON posts(chat_id, grouped_id);
CREATE TABLE IF NOT EXISTS channels(
  chat_id INTEGER PRIMARY KEY,
  read_mark INTEGER NOT NULL,
  new_cursor INTEGER NOT NULL,
  retro_cursor INTEGER NOT NULL,
  retro_until TEXT,
  phase TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL,
  mode TEXT NOT NULL,
  target TEXT NOT NULL,
  title TEXT,
  content TEXT NOT NULL,
  keep_image INTEGER NOT NULL DEFAULT 0,
  alts TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'drafted',
  card_msg_id INTEGER,
  sent_on TEXT,
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks(
  id INTEGER PRIMARY KEY,
  draft_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  start INTEGER NOT NULL,
  stop INTEGER NOT NULL,
  sha TEXT NOT NULL,
  extra TEXT NOT NULL DEFAULT '{}',
  created TEXT NOT NULL,
  rolled_back INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS labels(post_id INTEGER PRIMARY KEY, useful INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audits(
  id INTEGER PRIMARY KEY,
  block_id INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, text TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
JSON_FIELDS = {"media", "images", "top3", "alts", "extra"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _enc(k: str, v):
    return json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and not isinstance(v, str) else v


class DB:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA busy_timeout=30000")
        self.c.executescript(SCHEMA)

    def _update(self, table: str, key: str, kval, fields: dict) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.c.execute(f"UPDATE {table} SET {cols} WHERE {key}=?", (*(_enc(k, v) for k, v in fields.items()), kval))

    def _insert(self, table: str, fields: dict) -> int:
        cols, qs = ", ".join(fields), ", ".join("?" * len(fields))
        cur = self.c.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", tuple(_enc(k, v) for k, v in fields.items()))
        return cur.lastrowid

    # --- posts
    def add_post(self, chat_id: int, msg_id: int, grouped_id: int | None, date: str, text: str,
                 media_kind: str | None, source: str = "history") -> int | None:
        """Новый пост -> id. Уже был или это часть альбома -> None (часть вливается в пост альбома)."""
        media = [{"msg_id": msg_id, "kind": media_kind}] if media_kind else []
        if grouped_id is not None:
            row = self.c.execute("SELECT id, msg_id, text, media FROM posts WHERE chat_id=? AND grouped_id=?",
                                 (chat_id, grouped_id)).fetchone()
            if row is not None:
                have = json.loads(row["media"])
                if row["msg_id"] == msg_id or any(m["msg_id"] == msg_id for m in have):
                    return None
                self._update("posts", "id", row["id"], {"media": have + media, "text": row["text"] or text})
                return None
        cur = self.c.execute(
            "INSERT OR IGNORE INTO posts(chat_id, msg_id, grouped_id, date, text, media, source) VALUES(?,?,?,?,?,?,?)",
            (chat_id, msg_id, grouped_id, date, text, json.dumps(media), source))
        return cur.lastrowid if cur.rowcount else None

    def get_post(self, pid: int):
        return self.c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()

    def posts(self, status: str, limit: int = 100) -> list:
        return self.c.execute("SELECT * FROM posts WHERE status=? ORDER BY id LIMIT ?", (status, limit)).fetchall()

    def video_asks(self, limit: int = 10) -> list:
        return self.c.execute("SELECT * FROM posts WHERE status='video_ask' AND video_state='ask' ORDER BY id LIMIT ?",
                              (limit,)).fetchall()

    def videos_approved(self, limit: int = 5) -> list:
        return self.c.execute("SELECT * FROM posts WHERE status='video_ask' AND video_state='yes' ORDER BY id LIMIT ?",
                              (limit,)).fetchall()

    def update_post(self, pid: int, **fields) -> None:
        self._update("posts", "id", pid, fields)

    def bump_attempts(self, pid: int) -> int:
        self.c.execute("UPDATE posts SET attempts=attempts+1 WHERE id=?", (pid,))
        return self.get_post(pid)["attempts"]

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.c.execute("SELECT status, COUNT(*) FROM posts GROUP BY status")}

    def night_queue(self, explore_quota: int) -> list:
        cand = self.c.execute(
            "SELECT * FROM posts WHERE status='candidate' ORDER BY (source='live') DESC, score DESC, id").fetchall()
        expl = self.c.execute("SELECT * FROM posts WHERE status='explore' ORDER BY usefulness DESC, id LIMIT ?",
                              (explore_quota,)).fetchall()
        return list(cand) + list(expl)

    def pilot_sample(self, n: int) -> list:
        return self.c.execute(
            "SELECT * FROM posts WHERE score IS NOT NULL AND id NOT IN (SELECT post_id FROM labels) "
            "ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()

    # --- channels
    def channel(self, chat_id: int):
        return self.c.execute("SELECT * FROM channels WHERE chat_id=?", (chat_id,)).fetchone()

    def save_channel(self, chat_id: int, **fields) -> None:
        if self.channel(chat_id) is None:
            self._insert("channels", {"chat_id": chat_id, **fields})
        else:
            self._update("channels", "chat_id", chat_id, fields)

    def retro_active(self) -> bool:
        return self.c.execute("SELECT COUNT(*) FROM channels WHERE phase!='done'").fetchone()[0] > 0

    # --- drafts
    def add_draft(self, post_id: int, mode: str, target: str, title: str | None, content: str,
                  keep_image: bool, alts: list) -> int:
        return self._insert("drafts", {"post_id": post_id, "mode": mode, "target": target, "title": title,
                                       "content": content, "keep_image": int(keep_image), "alts": alts,
                                       "created": _now()})

    def get_draft(self, did: int):
        return self.c.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()

    def drafts(self, status: str, limit: int = 100) -> list:
        return self.c.execute("SELECT * FROM drafts WHERE status=? ORDER BY id LIMIT ?", (status, limit)).fetchall()

    def update_draft(self, did: int, **fields) -> None:
        self._update("drafts", "id", did, fields)

    # --- blocks
    def add_block(self, draft_id: int, path: str, kind: str, start: int, stop: int, sha: str, extra: dict) -> int:
        return self._insert("blocks", {"draft_id": draft_id, "path": path, "kind": kind, "start": start,
                                       "stop": stop, "sha": sha, "extra": extra, "created": _now()})

    def get_block(self, bid: int):
        return self.c.execute("SELECT * FROM blocks WHERE id=?", (bid,)).fetchone()

    def active_blocks(self, path: str) -> list:
        return self.c.execute("SELECT * FROM blocks WHERE path=? AND rolled_back=0 ORDER BY start", (path,)).fetchall()

    def blocks_since(self, iso: str) -> list:
        return self.c.execute("SELECT * FROM blocks WHERE created>=? AND rolled_back=0 ORDER BY id", (iso,)).fetchall()

    def shift_blocks(self, path: str, at: int, delta: int) -> None:
        self.c.execute("UPDATE blocks SET start=start+?, stop=stop+? WHERE path=? AND rolled_back=0 AND start>=?",
                       (delta, delta, path, at))

    def mark_rolled_back(self, bid: int) -> None:
        self.c.execute("UPDATE blocks SET rolled_back=1 WHERE id=?", (bid,))

    # --- labels
    def add_label(self, pid: int, useful: bool) -> None:
        self.c.execute("INSERT OR REPLACE INTO labels(post_id, useful) VALUES(?, ?)", (pid, int(useful)))

    def labeled(self) -> list:
        return self.c.execute("SELECT p.score, p.section_score, l.useful FROM labels l "
                              "JOIN posts p ON p.id = l.post_id").fetchall()

    # --- audits
    def add_audit(self, block_id: int, verdict: str, reason: str) -> int:
        return self._insert("audits", {"block_id": block_id, "verdict": verdict, "reason": reason})

    def get_audit(self, aid: int):
        return self.c.execute("SELECT * FROM audits WHERE id=?", (aid,)).fetchone()

    def audits(self, status: str, limit: int = 50) -> list:
        return self.c.execute("SELECT * FROM audits WHERE status=? ORDER BY id LIMIT ?", (status, limit)).fetchall()

    def update_audit(self, aid: int, **fields) -> None:
        self._update("audits", "id", aid, fields)

    # --- alerts
    def push_alert(self, text: str) -> None:
        self.c.execute("INSERT INTO alerts(text) VALUES(?)", (text,))

    def pop_alerts(self) -> list[str]:
        rows = self.c.execute("SELECT id, text FROM alerts WHERE sent=0 ORDER BY id").fetchall()
        if rows:
            self.c.execute(f"UPDATE alerts SET sent=1 WHERE id IN ({','.join('?' * len(rows))})",
                           [r["id"] for r in rows])
        return [r["text"] for r in rows]

    # --- kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, value))
```

- [x] **Step 7: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS (9 passed)

- [x] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(harvester): каркас, конфиг, БД

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 2: Vault-утилиты, снапшоты, Writer (дописать / новая / откат)

**Files:**
- Create: `harvester/vault/fs.py`, `harvester/vault/canvas.py` (копии из idea-bot), `harvester/vault/notes.py`, `harvester/files.py`, `harvester/writer.py`
- Test: `tests/test_writer.py`

**Interfaces:**
- Consumes: `DB`, `Settings`, `make_draft`, `CHAT_A` (задача 1)
- Produces:
  - `files.snapshot(s, path) -> Path | None`, `files.prune_snapshots(s, today: date | None = None) -> int`, `files.drop_keep(s, post_id) -> None`, `files.list_sections(vault) -> list[str]`, `files.find_moc(vault, section) -> Path | None`
  - `writer.TG_H = "## Из Telegram"`, `writer.WriteError`, `writer.post_link(chat_id, msg_id) -> str`, `writer.sha(bytes) -> str`
  - `Writer(s, db).apply(draft_id) -> block_id`, `Writer.rollback(block_id) -> "ok" | "changed" | "gone" | "already"`
  - Блок append хранит `extra = {"text": вставка целиком, "line": только строка, "image"?: rel}`; блок new: `extra = {"item", "moc"?, "canvas"?, "node"?, "image"?}`

- [x] **Step 1: Скопировать fs.py и canvas.py из idea-bot**

```bash
cd "/Users/cucaramba/Claude Code/tg-harvester"
cp ../idea-bot/ideabot/vault/fs.py harvester/vault/fs.py
cp ../idea-bot/ideabot/vault/canvas.py harvester/vault/canvas.py
sed -i '' 's/prefix=".ideabot-"/prefix=".harvester-"/' harvester/vault/fs.py
grep -n "harvester-" harvester/vault/fs.py
```

Expected: строка с `prefix=".harvester-"`. `canvas.py` импортирует только `.fs`, правок не нужно.

`harvester/vault/notes.py` (выжимка из idea-bot без моделей идей; двоеточие в имени файла заменяется на дефис, а не на длинное тире):

```python
"""Имена файлов заметок и пункты MOC."""
from __future__ import annotations

import re
from pathlib import Path

from .fs import atomic_write

_BAD = re.compile(r'[\\*?"<>|#^\[\]]')


def safe_filename(title: str) -> str:
    name = title.replace(":", " -").replace("/", "-")
    name = _BAD.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    return name[:100].rstrip() or "Заметка"


def unique_note_path(folder: Path, title: str) -> Path:
    base = safe_filename(title)
    path, n = folder / f"{base}.md", 2
    while path.exists():
        path, n = folder / f"{base} ({n}).md", n + 1
    return path


def moc_add(moc: Path, theme: str, item: str) -> None:
    text = moc.read_text("utf-8") if moc.exists() else f"# {moc.stem}\n"
    lines = text.splitlines()
    head = f"## {theme}"
    hi = next((i for i, line in enumerate(lines) if line.strip() == head), None)
    if hi is None:
        atomic_write(moc, text.rstrip("\n") + f"\n\n{head}\n\n- {item}\n")
        return
    end = hi + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    last = end
    while last > hi + 1 and not lines[last - 1].strip():
        last -= 1
    lines.insert(last, f"- {item}")
    atomic_write(moc, "\n".join(lines) + "\n")


def moc_remove(moc: Path, item: str) -> None:
    """Убирает пункт. Если в его разделе больше ничего нет, убирает и заголовок раздела."""
    if not moc.exists():
        return
    out = []
    for sec in re.split(r"(?m)^(?=## )", moc.read_text("utf-8")):
        lines = sec.splitlines(keepends=True)
        kept = [x for x in lines if x.strip() != f"- {item}"]
        if len(kept) != len(lines) and sec.startswith("## ") and not any(x.strip() for x in kept[1:]):
            continue
        out.append("".join(kept))
    atomic_write(moc, "".join(out).rstrip("\n") + "\n")
```

- [x] **Step 2: Падающие тесты Writer**

`tests/test_writer.py`:

```python
import json
from datetime import date

import pytest

from harvester.files import prune_snapshots
from harvester.writer import Writer, WriteError, post_link
from tests.helpers import CHAT_A, make_draft

NOTE = "---\ntags: [x]\n---\n# Промпты\n\nмой текст\n"
REL = "ИИ работа/Промпты.md"


def note(s, text=NOTE, rel=REL):
    p = s.vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    return p


def test_append_adds_heading_and_keeps_original_bytes(s, db):
    p = note(s)
    did = make_draft(db, 1, "append", REL)
    Writer(s, db).apply(did)
    out = p.read_text("utf-8")
    assert out == NOTE + f"\n## Из Telegram\n\n- суть поста ([пост]({post_link(CHAT_A, 1)}), 2026-10-01)\n"
    assert db.get_draft(did)["status"] == "written"
    assert db.get_post(db.get_draft(did)["post_id"])["status"] == "written"
    snap = s.snapshots_dir / date.today().isoformat() / REL
    assert snap.read_text("utf-8") == NOTE


def test_append_multiline_content_is_indented(s, db):
    p = note(s)
    Writer(s, db).apply(make_draft(db, 1, "append", REL, content="первая\nвторая"))
    assert p.read_text("utf-8").endswith("- первая\n  вторая\n  ([пост](https://t.me/c/1234/1), 2026-10-01)\n")


def test_append_goes_into_existing_heading_before_next_section(s, db):
    text = "# N\n\n## Из Telegram\n\n- старое\n\n## Ещё\n\nхвост\n"
    p = note(s, text)
    Writer(s, db).apply(make_draft(db, 1, "append", REL, content="новое"))
    out = p.read_text("utf-8")
    assert out.startswith("# N\n\n## Из Telegram\n\n- старое\n\n- новое (")
    assert out.endswith(")\n\n## Ещё\n\nхвост\n")
    assert out.count("## Из Telegram") == 1


def test_rollback_restores_exact_bytes(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    assert w.rollback(bid) == "ok"
    assert p.read_text("utf-8") == NOTE
    assert w.rollback(bid) == "already"
    assert db.get_draft(db.get_block(bid)["draft_id"])["status"] == "rolled_back"


def test_rollback_survives_edit_above_block(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    p.write_text("строка сверху\n" + p.read_text("utf-8"), "utf-8")
    assert w.rollback(bid) == "ok"
    assert p.read_text("utf-8") == "строка сверху\n" + NOTE


def test_rollback_refuses_when_block_was_edited(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    p.write_text(p.read_text("utf-8").replace("суть поста", "суть поста, поправил руками"), "utf-8")
    assert w.rollback(bid) == "changed"
    assert "поправил руками" in p.read_text("utf-8")


def test_rollback_first_of_two_keeps_heading_for_second(s, db):
    p = note(s)
    w = Writer(s, db)
    b1 = w.apply(make_draft(db, 1, "append", REL, content="первое"))
    b2 = w.apply(make_draft(db, 2, "append", REL, content="второе"))
    assert w.rollback(b1) == "ok"
    assert p.read_text("utf-8").startswith(NOTE + "\n## Из Telegram\n\n- второе (")
    assert w.rollback(b2) == "ok"
    assert p.read_text("utf-8") == NOTE + "\n## Из Telegram\n\n"


def test_missing_note_raises_and_draft_stays(s, db):
    did = make_draft(db, 1, "append", "ИИ работа/Нет.md")
    with pytest.raises(WriteError):
        Writer(s, db).apply(did)
    assert db.get_draft(did)["status"] == "drafted"


def test_target_outside_vault_is_rejected(s, db):
    (s.root / "evil.md").write_text("x", "utf-8")
    with pytest.raises(WriteError):
        Writer(s, db).apply(make_draft(db, 1, "append", "../evil.md"))
    with pytest.raises(WriteError):
        Writer(s, db).apply(make_draft(db, 2, "new", "..", title="x"))
    assert (s.root / "evil.md").read_text("utf-8") == "x"


def _music(s):
    sec = s.vault / "Музыка"
    sec.mkdir()
    (sec / "Аккорды.md").write_text("# Аккорды\n", "utf-8")
    moc = sec / "Музыка — карта заметок.md"
    moc.write_text("# Музыка — карта заметок\n", "utf-8")
    cvp = s.vault / "Музыка.canvas"
    cvp.write_text('{"nodes": [], "edges": []}', "utf-8")
    return sec, moc, cvp


def test_new_note_with_moc_canvas_and_rollback(s, db):
    sec, moc, cvp = _music(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 7, "new", "Музыка", content="Как свести трек", title="Основы сведения",
                             top3=[("Музыка/Аккорды.md", 0.7)]))
    n = sec / "Основы сведения.md"
    text = n.read_text("utf-8")
    assert "tags: [telegram]" in text and "# Основы сведения" in text and "Как свести трек" in text
    assert "[[Музыка/Аккорды|Аккорды]]" in text and post_link(CHAT_A, 7) in text
    assert "[[Музыка/Основы сведения|Основы сведения]]" in moc.read_text("utf-8")
    cv = json.loads(cvp.read_text("utf-8"))
    assert any(x.get("file") == "Музыка/Основы сведения.md" for x in cv["nodes"])
    assert w.rollback(bid) == "ok"
    assert not n.exists()
    assert moc.read_text("utf-8") == "# Музыка — карта заметок\n"
    assert json.loads(cvp.read_text("utf-8"))["nodes"] == []


def test_new_note_edited_by_hand_is_not_rolled_back(s, db):
    sec, _, _ = _music(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 7, "new", "Музыка", title="Сведение"))
    (sec / "Сведение.md").write_text("переписал", "utf-8")
    assert w.rollback(bid) == "changed"
    assert (sec / "Сведение.md").exists()


def test_keep_image_copies_attachment_and_rollback_removes_it(s, db, tmp_path):
    p = note(s)
    img = tmp_path / "keep" / "pic.jpg"
    img.parent.mkdir()
    img.write_bytes(b"jpg")
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 9, "append", REL, keep_image=True, images=[str(img)]))
    att = s.vault / "Вложения/Telegram/tg-1234-9.jpg"
    assert att.read_bytes() == b"jpg"
    assert "![[Вложения/Telegram/tg-1234-9.jpg]]" in p.read_text("utf-8")
    assert w.rollback(bid) == "ok"
    assert not att.exists() and p.read_text("utf-8") == NOTE


def test_prune_snapshots_drops_old_days(s):
    old = s.snapshots_dir / "2026-01-01"
    new = s.snapshots_dir / "2026-10-09"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    assert prune_snapshots(s, today=date(2026, 10, 10)) == 1
    assert not old.exists() and new.exists()
```

- [x] **Step 3: Убедиться, что падают**

Run: `uv run pytest tests/test_writer.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.files'`

- [x] **Step 4: `harvester/files.py`**

```python
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
```

- [x] **Step 5: `harvester/writer.py`**

```python
"""Запись одобренных черновиков в vault и откат. Существующий текст заметки не меняется никогда."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .config import Settings
from .db import DB
from .files import drop_keep, find_moc, snapshot
from .vault.canvas import Canvas, add_to_group, ensure_side_group
from .vault.fs import atomic_write, wikilink
from .vault.notes import moc_add, moc_remove, unique_note_path

TG_H = "## Из Telegram"
TG_GROUP = "📥 Из Telegram"
MOC_SECTION = "Из Telegram"


class WriteError(RuntimeError):
    pass


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def post_link(chat_id: int, msg_id: int) -> str:
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{msg_id}"


def render_line(content: str, link: str, day: str) -> str:
    lines = [x.rstrip() for x in content.strip().splitlines()] or ["(пусто)"]
    src = f"([пост]({link}), {day})"
    if len(lines) == 1:
        return f"- {lines[0]} {src}\n"
    body = "\n".join(f"  {x}" if x else "" for x in lines[1:])
    return f"- {lines[0]}\n{body}\n  {src}\n"


def plan_append(text: str, line: str) -> tuple[int, str]:
    """Куда вставить и что. Всё выше точки вставки остаётся как было."""
    m = re.search(rf"(?m)^{re.escape(TG_H)}[ \t]*$", text)
    if m is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        return len(text), f"{sep}\n{TG_H}\n\n{line}"
    nxt = re.search(r"(?m)^## ", text[m.end():])
    if nxt:
        return m.end() + nxt.start(), line + "\n"
    sep = "" if text.endswith("\n") else "\n"
    return len(text), sep + line


def inside(vault: Path, rel: str) -> Path:
    """Путь внутри vault или WriteError: Claude не должен уметь писать куда-то ещё."""
    if not rel or not (vault / rel).resolve().is_relative_to(vault.resolve()) or (vault / rel).resolve() == vault.resolve():
        raise WriteError(f"путь вне vault: {rel}")
    return vault / rel


class Writer:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db

    def apply(self, did: int) -> int:
        d = self.db.get_draft(did)
        if d is None or d["status"] not in ("drafted", "sent"):
            raise WriteError("черновик уже обработан")
        post = self.db.get_post(d["post_id"])
        link, day = post_link(post["chat_id"], post["msg_id"]), post["date"][:10]
        if d["mode"] == "append":
            path = inside(self.s.vault, d["target"])
            if not path.is_file():
                raise WriteError(f"заметки больше нет: {d['target']}")
        else:
            path = inside(self.s.vault, d["target"])
            if not path.is_dir():
                raise WriteError(f"раздела нет: {d['target']}")
        image = self._copy_image(post) if d["keep_image"] else None
        content = d["content"].strip() + (f"\n![[{image}]]" if image else "")
        try:
            if d["mode"] == "append":
                bid = self._append(d, path, content, link, day, image)
            else:
                bid = self._new(d, post, path, content, link, day, image)
        except Exception:
            if image:
                (self.s.vault / image).unlink(missing_ok=True)
            raise
        self.db.update_draft(did, status="written")
        self.db.update_post(post["id"], status="written")
        drop_keep(self.s, post["id"])
        return bid

    def _copy_image(self, post) -> str | None:
        for p in map(Path, json.loads(post["images"])):
            if p.exists():
                rel = f"{self.s.attachments_dir}/tg-{str(post['chat_id']).removeprefix('-100')}-{post['msg_id']}{p.suffix}"
                dest = self.s.vault / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
                return rel
        return None

    def _append(self, d, path: Path, content: str, link: str, day: str, image: str | None) -> int:
        snapshot(self.s, path)
        text = path.read_bytes().decode("utf-8")
        line = render_line(content, link, day)
        pos, ins = plan_append(text, line)
        atomic_write(path, text[:pos] + ins + text[pos:])
        start, size = len(text[:pos].encode()), len(ins.encode())
        self.db.shift_blocks(d["target"], start, size)
        extra = {"text": ins, "line": line, **({"image": image} if image else {})}
        return self.db.add_block(d["id"], d["target"], "append", start, start + size, sha(ins.encode()), extra)

    def _new(self, d, post, folder: Path, content: str, link: str, day: str, image: str | None) -> int:
        section = d["target"]
        title = (d["title"] or "Из Telegram").strip()
        path = unique_note_path(folder, title)
        rel = path.relative_to(self.s.vault).as_posix()
        related = [wikilink(r) for r, _ in json.loads(post["top3"]) if (self.s.vault / r).is_file()][:3]
        channel = self.s.channel_name(post["chat_id"])
        body = (f"---\nsource: {json.dumps(link)}\nchannel: {json.dumps(channel, ensure_ascii=False)}\n"
                f"date: {day}\ntags: [telegram]\n---\n# {title}\n\n{content}\n\nИсточник: [пост]({link}), {channel}\n")
        if related:
            body += "\n## Связанные заметки\n" + "".join(f"- {x}\n" for x in related)
        atomic_write(path, body)
        extra: dict = {"item": wikilink(rel), **({"image": image} if image else {})}
        if moc := find_moc(self.s.vault, section):
            snapshot(self.s, moc)
            moc_add(moc, MOC_SECTION, wikilink(rel))
            extra["moc"] = moc.relative_to(self.s.vault).as_posix()
        cv_path = self.s.vault / f"{section}.canvas"
        if cv_path.exists():
            snapshot(self.s, cv_path)
            cv = Canvas.load(cv_path)
            node = add_to_group(cv, ensure_side_group(cv, TG_GROUP), rel)
            cv.save()
            extra["canvas"], extra["node"] = cv_path.name, node["id"]
        data = path.read_bytes()
        return self.db.add_block(d["id"], rel, "new", 0, len(data), sha(data), extra)

    def rollback(self, bid: int) -> str:
        b = self.db.get_block(bid)
        if b is None or b["rolled_back"]:
            return "already"
        extra = json.loads(b["extra"])
        path = self.s.vault / b["path"]
        res = self._rollback_append(b, path, extra) if b["kind"] == "append" else self._rollback_new(b, path, extra)
        if res == "ok":
            if img := extra.get("image"):
                (self.s.vault / img).unlink(missing_ok=True)
            self.db.mark_rolled_back(bid)
            self.db.update_draft(b["draft_id"], status="rolled_back")
        return res

    def _rollback_append(self, b, path: Path, extra: dict) -> str:
        if not path.exists():
            return "gone"
        raw = path.read_bytes()
        ins, line = extra["text"].encode(), extra["line"].encode()
        start = b["start"]
        if raw[start:start + len(ins)] != ins:
            if raw.count(ins) != 1:
                return "changed"
            start = raw.index(ins)
        cut_from, cut_to = start, start + len(ins)
        later = [o for o in self.db.active_blocks(b["path"]) if o["id"] != b["id"] and o["start"] >= b["stop"]]
        if later and ins != line and ins.endswith(line):  # заголовок создал этот блок, но под ним уже есть другие
            cut_from = cut_to - len(line)
        snapshot(self.s, path)
        atomic_write(path, (raw[:cut_from] + raw[cut_to:]).decode("utf-8"))
        self.db.shift_blocks(b["path"], cut_to, -(cut_to - cut_from))
        return "ok"

    def _rollback_new(self, b, path: Path, extra: dict) -> str:
        if path.exists():
            if sha(path.read_bytes()) != b["sha"]:
                return "changed"
            snapshot(self.s, path)
            path.unlink()
        if moc_rel := extra.get("moc"):
            moc_remove(self.s.vault / moc_rel, extra["item"])
        if cv_name := extra.get("canvas"):
            cv_path = self.s.vault / cv_name
            if cv_path.exists():
                cv = Canvas.load(cv_path)
                cv.remove_node(extra["node"])
                g = cv.group(TG_GROUP)
                if g and not cv.children(g):
                    cv.remove_node(g["id"])
                cv.save()
        return "ok"
```

- [x] **Step 6: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS (все тесты задач 1-2)

- [x] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(harvester): vault-утилиты, снапшоты, Writer с откатом

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Раздел L2 «Музыка» (ensure_section)

**Files:**
- Create: `harvester/vault_setup.py`
- Test: `tests/test_vault_setup.py`

**Interfaces:**
- Consumes: `Canvas`, `atomic_write` (задача 2)
- Produces: `ensure_section(vault: Path, name: str) -> list[str]` (пути созданного/изменённого; пусто, если всё уже было). Вызывается из CLI `section` (задача 10).

Формат «Обо мне.canvas»: группа `Сферы жизни (2-й уровень)` (x=0, y=1080), внутри file-ноды L2-холстов 440x180 сеткой по 5 в ряд, шаг 460 по x и 200 по y, отступ 30 слева и 60 сверху.

- [ ] **Step 1: Падающий тест**

`tests/test_vault_setup.py`:

```python
import json

from harvester.vault_setup import ROOT_CANVAS, ROOT_GROUP, ensure_section


def _root(vault):
    nodes = [{"id": "g", "type": "group", "label": ROOT_GROUP, "x": 0, "y": 1080, "width": 2400, "height": 460}]
    nodes += [{"id": f"n{i}", "type": "file", "file": f"L{i}.canvas", "x": 30 + (i % 5) * 460,
               "y": 1140 + (i // 5) * 200, "width": 440, "height": 180} for i in range(10)]
    (vault / ROOT_CANVAS).write_text(json.dumps({"nodes": nodes, "edges": []}), "utf-8")


def test_ensure_section_creates_folder_moc_canvas_and_root_link(tmp_path):
    _root(tmp_path)
    created = ensure_section(tmp_path, "Музыка")
    assert created == ["Музыка/Музыка — карта заметок.md", "Музыка.canvas", ROOT_CANVAS]
    assert (tmp_path / "Музыка" / "Музыка — карта заметок.md").read_text("utf-8") == "# Музыка — карта заметок\n"
    cv = json.loads((tmp_path / "Музыка.canvas").read_text("utf-8"))
    assert [n["file"] for n in cv["nodes"]] == ["Музыка/Музыка — карта заметок.md"]
    root = json.loads((tmp_path / ROOT_CANVAS).read_text("utf-8"))
    n = next(x for x in root["nodes"] if x.get("file") == "Музыка.canvas")
    assert (n["x"], n["y"], n["width"], n["height"]) == (30, 1540, 440, 180)
    g = next(x for x in root["nodes"] if x["id"] == "g")
    assert g["height"] == 1540 + 180 + 40 - 1080


def test_ensure_section_is_idempotent_and_works_without_root(tmp_path):
    assert ensure_section(tmp_path, "Музыка") == ["Музыка/Музыка — карта заметок.md", "Музыка.canvas"]
    assert ensure_section(tmp_path, "Музыка") == []
    _root(tmp_path)
    assert ensure_section(tmp_path, "Музыка") == [ROOT_CANVAS]
    assert ensure_section(tmp_path, "Музыка") == []
```

- [ ] **Step 2: Убедиться, что падает**

Run: `uv run pytest tests/test_vault_setup.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.vault_setup'`

- [ ] **Step 3: `harvester/vault_setup.py`**

```python
"""Новый раздел L2: папка, MOC, холст и file-нода в «Обо мне» (корень L1)."""
from __future__ import annotations

from pathlib import Path

from .vault.canvas import Canvas
from .vault.fs import atomic_write

ROOT_CANVAS = "Обо мне.canvas"
ROOT_GROUP = "Сферы жизни (2-й уровень)"
COLS, CELL_W, CELL_H, STEP_X, STEP_Y, PAD_X, PAD_TOP, PAD_BOTTOM = 5, 440, 180, 460, 200, 30, 60, 40


def ensure_section(vault: Path, name: str) -> list[str]:
    created: list[str] = []
    moc_rel = f"{name}/{name} — карта заметок.md"
    moc = vault / moc_rel
    if not moc.exists():
        atomic_write(moc, f"# {name} — карта заметок\n")
        created.append(moc_rel)
    cv_path = vault / f"{name}.canvas"
    if not cv_path.exists():
        cv = Canvas.load(cv_path)
        cv.add_node(type="file", file=moc_rel, x=0, y=0, width=400, height=120)
        cv.save()
        created.append(cv_path.name)
    root = vault / ROOT_CANVAS
    if root.exists():
        cv = Canvas.load(root)
        g = cv.group(ROOT_GROUP)
        if g is not None and cv.file_node(f"{name}.canvas") is None:
            i = len(cv.children(g))
            x = g["x"] + PAD_X + (i % COLS) * STEP_X
            y = g["y"] + PAD_TOP + (i // COLS) * STEP_Y
            cv.add_node(type="file", file=f"{name}.canvas", x=x, y=y, width=CELL_W, height=CELL_H)
            g["height"] = max(g["height"], y + CELL_H + PAD_BOTTOM - g["y"])
            cv.save()
            created.append(ROOT_CANVAS)
    return created
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(harvester): создание раздела L2 со связью в «Обо мне»

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Правила и сборщик (фазы «новое» → ретро, альбомы, preflight, живые посты)

**Files:**
- Create: `harvester/rules.py`, `harvester/collector.py`
- Test: `tests/test_rules.py`, `tests/test_collector.py`

**Interfaces:**
- Consumes: `DB.add_post/channel/save_channel/push_alert/kv_set`, `Settings.channels/retro_until/retro_cap/retro_fallback_days/chunk/request_pause`
- Produces:
  - `rules.ad_reason(text: str, markers: list[str]) -> str | None`
  - `collector.media_kind(msg) -> "photo" | "voice" | "video" | "audio" | None`
  - `collector.TgClient(telethon_client)`: `read_mark(chat_id) -> int`, `iter_messages(chat_id, **kw)` (async-итератор Telethon), `first_before(chat_id, when: datetime) -> int | None`, `download(chat_id, msg_id, dest: Path) -> Path | None`
  - `Collector(s, db, tg, today=date.today)`: `init()`, `step() -> bool` (False, когда все каналы `done`), `on_new(event)` (обработчик Telethon `events.NewMessage`)

Логика курсоров: `new_cursor` = последний собранный id в фазе «новое» (стартует с отметки прочитанного, но не ниже первого поста до `retro_until`); `retro_cursor` = id, ниже которого идём в ретро (`max_id` у Telethon исключающий). Пока у любого канала фаза `new`, ретро не начинается ни у кого.

- [ ] **Step 1: Падающие тесты**

`tests/test_rules.py`:

```python
from harvester.rules import ad_reason

M = ["erid", "#реклама", "розыгрыш"]


def test_ad_markers_case_insensitive():
    assert ad_reason("Реклама. ERID: 2Vtzq", M) == "реклама: erid"
    assert ad_reason("Большой РОЗЫГРЫШ айфонов", M) == "реклама: розыгрыш"


def test_plain_post_passes():
    assert ad_reason("Гайд по промптам для Claude", M) is None
    assert ad_reason("", M) is None
```

`tests/test_collector.py`:

```python
import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from harvester.collector import Collector, media_kind
from tests.helpers import CHAT_A, CHAT_B, make_settings

UTC = timezone.utc


def msg(i, when, text="", grouped=None, kind=None):
    m = SimpleNamespace(id=i, date=when, message=text, grouped_id=grouped,
                        photo=None, voice=None, video=None, video_note=None, audio=None)
    if kind:
        setattr(m, kind, object())
    return m


def d(y, mo, day):
    return datetime(y, mo, day, tzinfo=UTC)


class FakeTg:
    def __init__(self, msgs, marks):
        self.msgs, self.marks = msgs, marks

    async def read_mark(self, chat_id):
        return self.marks[chat_id]

    async def iter_messages(self, chat_id, limit, min_id=0, max_id=0, reverse=False):
        ms = sorted(self.msgs[chat_id], key=lambda m: m.id, reverse=not reverse)
        if min_id:
            ms = [m for m in ms if m.id > min_id]
        if max_id:
            ms = [m for m in ms if m.id < max_id]
        for m in ms[:limit]:
            yield m

    async def first_before(self, chat_id, when):
        older = [m.id for m in self.msgs[chat_id] if m.date < when]
        return max(older) if older else None


def run_all(c, max_steps=50):
    asyncio.run(c.init())
    n = 0
    while asyncio.run(c.step()):
        n += 1
        assert n < max_steps
    return n


def ids(db, chat):
    return sorted(r["msg_id"] for r in db.c.execute("SELECT msg_id FROM posts WHERE chat_id=?", (chat,)))


A = [msg(1, d(2025, 9, 1), "старый"), msg(2, d(2025, 10, 1), "до границы"), msg(3, d(2025, 10, 15), "ретро 3"),
     msg(4, d(2026, 1, 1), "ретро 4"), msg(5, d(2026, 5, 1), "ретро 5"), msg(6, d(2026, 9, 1), "новый 6"),
     msg(7, d(2026, 9, 2), "новый 7"), msg(8, d(2026, 9, 3), "новый 8")]
B = [msg(1, d(2026, 1, 1)), msg(2, d(2026, 2, 1)), msg(3, d(2026, 9, 1), "б3"), msg(4, d(2026, 9, 2), "б4")]


def test_media_kind():
    assert media_kind(msg(1, d(2026, 1, 1), kind="photo")) == "photo"
    assert media_kind(msg(1, d(2026, 1, 1), kind="video_note")) == "video"
    assert media_kind(msg(1, d(2026, 1, 1))) is None


def test_new_phase_covers_all_channels_before_any_retro(s, db):
    c = Collector(s, db, FakeTg({CHAT_A: A, CHAT_B: B}, {CHAT_A: 5, CHAT_B: 2}))
    asyncio.run(c.init())
    for _ in range(3):
        asyncio.run(c.step())
    assert db.channel(CHAT_A)["phase"] == "retro"
    assert db.channel(CHAT_B)["phase"] == "done"  # backfill: false
    assert ids(db, CHAT_A) == [6, 7, 8] and ids(db, CHAT_B) == [3, 4]


def test_retro_stops_at_boundary(s, db):
    run_all(Collector(s, db, FakeTg({CHAT_A: A, CHAT_B: B}, {CHAT_A: 5, CHAT_B: 2})))
    assert ids(db, CHAT_A) == [3, 4, 5, 6, 7, 8]
    assert db.channel(CHAT_A)["phase"] == "done" and db.channel(CHAT_A)["retro_until"] == "2025-10-10"
    assert db.retro_active() is False


def test_preflight_cap_cuts_retro_to_fallback_window(tmp_path, db):
    s = make_settings(tmp_path, retro_cap=1, retro_fallback_days=60)
    a = [msg(1, d(2025, 11, 1), "1"), msg(2, d(2026, 3, 1), "2"), msg(3, d(2026, 8, 1), "3"),
         msg(4, d(2026, 9, 1), "4"), msg(5, d(2026, 9, 15), "5"), msg(6, d(2026, 10, 1), "6")]
    run_all(Collector(s, db, FakeTg({CHAT_A: a, CHAT_B: []}, {CHAT_A: 6, CHAT_B: 0}), today=lambda: date(2026, 10, 10)))
    assert ids(db, CHAT_A) == [4, 5, 6]
    assert db.channel(CHAT_A)["retro_until"] == "2026-08-11"
    assert any("урезано" in t for t in db.pop_alerts())
    assert json.loads(db.kv_get(f"preflight:{CHAT_A}"))["until"] == "2026-08-11"


def test_zero_read_mark_does_not_crawl_years(s, db):
    a = [msg(1, d(2024, 1, 1), "давно"), msg(2, d(2025, 6, 1), "давно 2"), msg(3, d(2025, 11, 1), "свежий")]
    run_all(Collector(s, db, FakeTg({CHAT_A: a, CHAT_B: []}, {CHAT_A: 0, CHAT_B: 0})))
    assert ids(db, CHAT_A) == [3]


def test_album_from_history_becomes_one_post(s, db):
    a = [msg(7, d(2026, 9, 1), "подпись", grouped=99, kind="photo"), msg(8, d(2026, 9, 1), "", grouped=99, kind="photo")]
    run_all(Collector(s, db, FakeTg({CHAT_A: a, CHAT_B: []}, {CHAT_A: 6, CHAT_B: 0})))
    rows = db.c.execute("SELECT * FROM posts").fetchall()
    assert len(rows) == 1 and rows[0]["text"] == "подпись"
    assert [m["msg_id"] for m in json.loads(rows[0]["media"])] == [7, 8]


def test_restart_mid_phase_does_not_duplicate(s, db):
    tg = FakeTg({CHAT_A: A, CHAT_B: B}, {CHAT_A: 5, CHAT_B: 2})
    c = Collector(s, db, tg)
    asyncio.run(c.init())
    asyncio.run(c.step())
    db.save_channel(CHAT_A, new_cursor=5)  # как будто упали до сохранения курсора
    c2 = Collector(s, db, tg)
    asyncio.run(c2.init())  # канал уже есть, init его не трогает
    asyncio.run(c2.step())
    assert ids(db, CHAT_A) == [6, 7, 8]
    assert db.counts() == {"new": 3}


def test_live_message_is_stored_with_source_live(s, db):
    c = Collector(s, db, FakeTg({CHAT_A: [], CHAT_B: []}, {CHAT_A: 0, CHAT_B: 0}))
    asyncio.run(c.on_new(SimpleNamespace(chat_id=CHAT_A, message=msg(50, d(2026, 10, 10), "живой", kind="photo"))))
    row = db.c.execute("SELECT * FROM posts").fetchone()
    assert (row["msg_id"], row["source"], json.loads(row["media"])[0]["kind"]) == (50, "live", "photo")
```

- [ ] **Step 2: Убедиться, что падают**

Run: `uv run pytest tests/test_rules.py tests/test_collector.py -q`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: `harvester/rules.py`**

```python
"""Дешёвые правила до любой обработки: реклама и розыгрыши."""
from __future__ import annotations


def ad_reason(text: str, markers: list[str]) -> str | None:
    low = (text or "").lower()
    return next((f"реклама: {m.lower()}" for m in markers if m.lower() in low), None)
```

- [ ] **Step 4: `harvester/collector.py`**

```python
"""Чтение каналов: фаза «новое» по всем каналам, потом ретро канал за каналом, плюс живые события.

В Telegram ничего не отмечается прочитанным, прогресс живёт в SQLite."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .config import Channel, Settings
from .db import DB

log = logging.getLogger(__name__)
UTC = timezone.utc


def media_kind(m) -> str | None:
    if getattr(m, "photo", None):
        return "photo"
    if getattr(m, "voice", None):
        return "voice"
    if getattr(m, "video", None) or getattr(m, "video_note", None):
        return "video"
    if getattr(m, "audio", None):
        return "audio"
    return None


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(), UTC)


class TgClient:
    """Тонкая обёртка над Telethon: всё, что нужно сборщику и обогатителю."""

    def __init__(self, client):
        self.c = client

    async def read_mark(self, chat_id: int) -> int:
        from telethon import functions, types
        peer = types.InputDialogPeer(peer=await self.c.get_input_entity(chat_id))
        res = await self.c(functions.messages.GetPeerDialogsRequest(peers=[peer]))
        return res.dialogs[0].read_inbox_max_id

    def iter_messages(self, chat_id: int, **kw):
        return self.c.iter_messages(chat_id, **kw)

    async def first_before(self, chat_id: int, when: datetime) -> int | None:
        ms = await self.c.get_messages(chat_id, limit=1, offset_date=when)
        return ms[0].id if ms else None

    async def download(self, chat_id: int, msg_id: int, dest: Path) -> Path | None:
        m = await self.c.get_messages(chat_id, ids=msg_id)
        if m is None or not m.media:
            return None
        p = await self.c.download_media(m, file=str(dest) + "/")
        return Path(p) if p else None


class Collector:
    def __init__(self, s: Settings, db: DB, tg, today=date.today):
        self.s, self.db, self.tg, self.today = s, db, tg, today

    async def init(self) -> None:
        floor_at = _midnight(self.s.retro_until)
        for ch in self.s.channels:
            if self.db.channel(ch.id) is not None:
                continue
            mark = await self.tg.read_mark(ch.id)
            floor = await self.tg.first_before(ch.id, floor_at) or 0
            mark = max(mark, floor)  # никогда не открывал канал: не ползём по годам истории
            self.db.save_channel(ch.id, read_mark=mark, new_cursor=mark, retro_cursor=mark + 1,
                                 retro_until=None, phase="new")
            log.info("канал %s: старт с id %s", ch.name, mark)

    def _next(self) -> tuple[Channel, object] | None:
        for phase in ("new", "retro"):
            for ch in self.s.channels:
                st = self.db.channel(ch.id)
                if st is not None and st["phase"] == phase:
                    return ch, st
        return None

    async def step(self) -> bool:
        nxt = self._next()
        if nxt is None:
            return False
        ch, st = nxt
        if st["phase"] == "new":
            await self._step_new(ch, st)
        else:
            await self._step_retro(ch, st)
        if self.s.request_pause:
            await asyncio.sleep(self.s.request_pause)
        return True

    async def _step_new(self, ch: Channel, st) -> None:
        cursor, n = st["new_cursor"], 0
        async for m in self.tg.iter_messages(ch.id, limit=self.s.chunk, min_id=cursor, reverse=True):
            self._store(ch.id, m, "history")
            cursor, n = max(cursor, m.id), n + 1
        fields: dict = {"new_cursor": cursor}
        if n < self.s.chunk:
            fields["phase"] = "retro" if ch.backfill else "done"
            log.info("канал %s: новое дочитано", ch.name)
        self.db.save_channel(ch.id, **fields)

    async def _step_retro(self, ch: Channel, st) -> None:
        until = date.fromisoformat(st["retro_until"]) if st["retro_until"] else await self._preflight(ch, st)
        until_at = _midnight(until)
        cursor, n, done = st["retro_cursor"], 0, False
        async for m in self.tg.iter_messages(ch.id, limit=self.s.chunk, max_id=cursor):
            n += 1
            if m.date < until_at:
                done = True
                break
            self._store(ch.id, m, "history")
            cursor = min(cursor, m.id)
        fields: dict = {"retro_cursor": cursor}
        if done or n < self.s.chunk:
            fields["phase"] = "done"
            log.info("канал %s: ретро закончено", ch.name)
        self.db.save_channel(ch.id, **fields)

    async def _preflight(self, ch: Channel, st) -> date:
        until = self.s.retro_until
        edge = await self.tg.first_before(ch.id, _midnight(until)) or 0
        count = st["retro_cursor"] - edge
        if count > self.s.retro_cap:
            until = self.today() - timedelta(days=self.s.retro_fallback_days)
            self.db.push_alert(f"✂️ {ch.name}: за год около {count} постов, ретро урезано до {until.isoformat()}")
        self.db.kv_set(f"preflight:{ch.id}", json.dumps({"count": count, "until": until.isoformat()}))
        self.db.save_channel(ch.id, retro_until=until.isoformat())
        return until

    def _store(self, chat_id: int, m, source: str) -> None:
        self.db.add_post(chat_id, m.id, m.grouped_id, m.date.isoformat(), m.message or "", media_kind(m), source)

    async def on_new(self, event) -> None:
        self._store(event.chat_id, event.message, "live")
```

- [ ] **Step 5: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(harvester): правила и сборщик каналов

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Обогатитель (OCR, whisper, диск, видео по запросу)

**Files:**
- Create: `harvester/stt.py`, `harvester/enricher.py`
- Test: `tests/test_enricher.py`

**Interfaces:**
- Consumes: `DB.update_post/bump_attempts/push_alert/kv_*`, `rules.ad_reason`, `files.drop_keep`, `TgClient.download(chat_id, msg_id, dest) -> Path | None`
- Produces:
  - `stt.transcribe(s, audio: Path) -> str` (async)
  - `enricher.free_gb(path=Path.home()) -> float`, `enricher.ocr_image(path) -> str`, `enricher.extract_video(video, out_dir) -> (audio | None, [frames])` (async)
  - `Enricher(s, db, tg, *, ocr=ocr_image, stt=None, disk=free_gb, extract=extract_video)`: `process(post) -> status` (`filtered | enriched | new | error`), `analyze_video(post) -> None`
  - Картинки поста лежат в `data/keep/<post_id>/` до финального статуса, пути в `posts.images`.

- [ ] **Step 1: Проверить OCR на маке (вручную, 1 минута)**

```bash
cd "/Users/cucaramba/Claude Code/tg-harvester"
uv run python -c "from ocrmac import ocrmac; import inspect; print(inspect.signature(ocrmac.OCR.__init__))"
```

Expected: в сигнатуре есть `language_preference`. Если параметр называется иначе, поправь вызов в `ocr_image` и запиши отличие в HANDOFF «Отклонения от плана».

- [ ] **Step 2: `harvester/stt.py` (копия idea-bot с другим initial prompt)**

```python
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
```

- [ ] **Step 3: Падающие тесты**

`tests/test_enricher.py`:

```python
import asyncio
import json
from pathlib import Path

from harvester.enricher import Enricher
from tests.helpers import CHAT_A

LONG_OCR = "текст с картинки про промпты для claude и нейросети"


class FakeTg:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def download(self, chat_id, msg_id, dest):
        if self.fail:
            raise RuntimeError("сеть упала")
        self.calls.append(msg_id)
        p = Path(dest) / f"{msg_id}.bin"
        p.write_bytes(b"x")
        return p


async def fake_stt(path):
    assert path.exists()
    return "расшифровка голосового про бег и колени"


def enr(s, db, tg=None, ocr=lambda p: LONG_OCR, disk=lambda: 50.0, extract=None):
    return Enricher(s, db, tg or FakeTg(), ocr=ocr, stt=fake_stt, disk=disk, extract=extract)


def post(db, msg_id, text, kind=None):
    pid = db.add_post(CHAT_A, msg_id, None, "2026-10-01T00:00:00+00:00", text, kind)
    return db.get_post(pid)


def test_ad_is_filtered_before_any_download(s, db):
    tg = FakeTg()
    p = post(db, 1, "Розыгрыш iPhone, подпишись", "photo")
    assert asyncio.run(enr(s, db, tg).process(p)) == "filtered"
    assert tg.calls == [] and db.get_post(p["id"])["reason"] == "реклама: розыгрыш"


def test_photo_is_ocred_and_kept(s, db):
    p = post(db, 2, "Смотри", "photo")
    assert asyncio.run(enr(s, db).process(p)) == "enriched"
    row = db.get_post(p["id"])
    assert row["extra_text"] == LONG_OCR
    imgs = json.loads(row["images"])
    assert len(imgs) == 1 and Path(imgs[0]).exists() and str(s.keep_dir) in imgs[0]


def test_short_post_without_ocr_is_filtered_and_keep_removed(s, db):
    p = post(db, 3, "Шедевр", "photo")
    assert asyncio.run(enr(s, db, ocr=lambda p: "").process(p)) == "filtered"
    assert db.get_post(p["id"])["reason"] == "short"
    assert not (s.keep_dir / str(p["id"])).exists()


def test_low_disk_skips_media_and_alerts_once_a_day(s, db):
    tg = FakeTg()
    e = enr(s, db, tg, disk=lambda: 1.0)
    p1 = post(db, 4, "Длинный пост про промпты для нейросетей без картинки", "photo")
    p2 = post(db, 5, "Ещё один длинный пост про промпты и нейросети", "photo")
    assert asyncio.run(e.process(p1)) == "enriched"
    assert asyncio.run(e.process(p2)) == "enriched"
    assert tg.calls == []
    assert len([t for t in db.pop_alerts() if "диске" in t]) == 1


def test_voice_is_transcribed_and_temp_removed(s, db):
    p = post(db, 6, "", "voice")
    assert asyncio.run(enr(s, db).process(p)) == "enriched"
    assert db.get_post(p["id"])["extra_text"] == "расшифровка голосового про бег и колени"
    assert list(s.tmp_dir.iterdir()) == []


def test_errors_retry_then_mark_error(s, db):
    e = enr(s, db, FakeTg(fail=True))
    p = post(db, 7, "Длинный пост с картинкой про нейросети", "photo")
    assert asyncio.run(e.process(p)) == "new"
    assert asyncio.run(e.process(db.get_post(p["id"]))) == "new"
    assert asyncio.run(e.process(db.get_post(p["id"]))) == "error"
    assert db.get_post(p["id"])["status"] == "error"


def test_analyze_video_adds_transcript_and_frames(s, db):
    async def extract(video, out):
        audio = Path(out) / "a.wav"
        audio.write_bytes(b"w")
        frame = Path(out) / "frame_1.jpg"
        frame.write_bytes(b"f")
        return audio, [frame]

    p = post(db, 8, "Видео про промпты", "video")
    db.update_post(p["id"], status="video_ask", video_state="yes", extra_text="было")
    asyncio.run(enr(s, db, extract=extract).analyze_video(db.get_post(p["id"])))
    row = db.get_post(p["id"])
    assert row["status"] == "enriched" and row["video_state"] == "done"
    assert row["extra_text"] == f"было\n\nрасшифровка голосового про бег и колени\n\n{LONG_OCR}"


def test_analyze_video_failure_does_not_get_stuck(s, db):
    p = post(db, 9, "Видео про промпты", "video")
    db.update_post(p["id"], status="video_ask", video_state="yes")
    asyncio.run(enr(s, db, FakeTg(fail=True)).analyze_video(db.get_post(p["id"])))
    row = db.get_post(p["id"])
    assert (row["status"], row["video_state"]) == ("enriched", "done")
    assert any("видео" in t for t in db.pop_alerts())
```

- [ ] **Step 4: Убедиться, что падают**

Run: `uv run pytest tests/test_enricher.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.enricher'`

- [ ] **Step 5: `harvester/enricher.py`**

```python
"""Обогащение без токенов: правила, OCR картинок (macOS Vision), whisper для аудио, видео только по запросу."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from datetime import date
from pathlib import Path

from .config import Settings
from .db import DB
from .files import drop_keep
from .rules import ad_reason
from .stt import transcribe

log = logging.getLogger(__name__)


def free_gb(path: Path = Path.home()) -> float:
    return shutil.disk_usage(path).free / 2**30


def ocr_image(path: Path) -> str:
    from ocrmac import ocrmac
    res = ocrmac.OCR(str(path), language_preference=["ru-RU", "en-US"]).recognize()
    return "\n".join(text for text, conf, _box in res if conf >= 0.3).strip()


async def _ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-loglevel", "error", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), 600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='ignore')[-300:]}")


async def extract_video(video: Path, out: Path) -> tuple[Path | None, list[Path]]:
    audio = out / "audio.wav"
    try:
        await _ffmpeg("-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio))
    except RuntimeError:
        audio = None  # видео без звуковой дорожки
    await _ffmpeg("-i", str(video), "-vf", "fps=1/10", "-frames:v", "3", str(out / "frame_%d.jpg"))
    return (audio if audio and audio.exists() and audio.stat().st_size > 1000 else None), sorted(out.glob("frame_*.jpg"))


class Enricher:
    def __init__(self, s: Settings, db: DB, tg, *, ocr=ocr_image, stt=None, disk=free_gb, extract=extract_video):
        self.s, self.db, self.tg = s, db, tg
        self.ocr, self.disk, self.extract = ocr, disk, extract
        self.stt = stt or (lambda p: transcribe(s, p))

    async def process(self, post) -> str:
        try:
            return await self._process(post)
        except Exception as e:
            log.exception("enrich post %s", post["id"])
            drop_keep(self.s, post["id"])
            if self.db.bump_attempts(post["id"]) >= 3:
                self.db.update_post(post["id"], status="error", reason=f"enrich: {e}"[:300])
                return "error"
            return "new"

    def _disk_ok(self) -> bool:
        if self.disk() >= self.s.min_free_gb:
            return True
        today = date.today().isoformat()
        if self.db.kv_get("disk_alert") != today:
            self.db.kv_set("disk_alert", today)
            self.db.push_alert(f"💾 На диске меньше {self.s.min_free_gb:g} GB: медиа пропускаю, текст обрабатываю")
        return False

    async def _process(self, post) -> str:
        pid, text = post["id"], post["text"] or ""
        if reason := ad_reason(text, self.s.ad_markers):
            self.db.update_post(pid, status="filtered", reason=reason)
            return "filtered"
        media = json.loads(post["media"])
        photos = [m for m in media if m["kind"] == "photo"][: self.s.max_images]
        sounds = [m for m in media if m["kind"] in ("voice", "audio")]
        extra: list[str] = []
        images: list[str] = []
        if (photos or sounds) and self._disk_ok():
            keep = self.s.keep_dir / str(pid)
            for m in photos:
                keep.mkdir(parents=True, exist_ok=True)
                p = await self.tg.download(post["chat_id"], m["msg_id"], keep)
                if p is None:
                    continue
                images.append(str(p))
                if t := await asyncio.to_thread(self.ocr, p):
                    extra.append(t)
            for m in sounds:
                extra += await self._transcribe(post["chat_id"], m["msg_id"])
        full = "\n".join([text, *extra]).strip()
        if len(full) < self.s.min_chars:
            self.db.update_post(pid, status="filtered", reason="short")
            drop_keep(self.s, pid)
            return "filtered"
        self.db.update_post(pid, status="enriched", extra_text="\n\n".join(extra), images=images)
        return "enriched"

    async def _transcribe(self, chat_id: int, msg_id: int) -> list[str]:
        self.s.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.s.tmp_dir) as d:
            p = await self.tg.download(chat_id, msg_id, Path(d))
            if p is None:
                return []
            t = await self.stt(p)
            return [t] if t else []

    async def analyze_video(self, post) -> None:
        """По кнопке «Разобрать»: звук -> whisper, 3 кадра -> OCR. При сбое идём дальше по подписи."""
        extra: list[str] = []
        try:
            if self._disk_ok():
                vids = [m for m in json.loads(post["media"]) if m["kind"] == "video"][:1]
                self.s.tmp_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=self.s.tmp_dir) as d:
                    for m in vids:
                        p = await self.tg.download(post["chat_id"], m["msg_id"], Path(d))
                        if p is None:
                            continue
                        audio, frames = await self.extract(p, Path(d))
                        if audio and (t := await self.stt(audio)):
                            extra.append(t)
                        for f in frames:
                            if t := await asyncio.to_thread(self.ocr, f):
                                extra.append(t)
        except Exception as e:
            log.exception("video post %s", post["id"])
            self.db.push_alert(f"🎬 Не смог разобрать видео поста {post['id']}: {e}"[:300])
        merged = "\n\n".join(x for x in [post["extra_text"], *extra] if x)
        self.db.update_post(post["id"], extra_text=merged, video_state="done", status="enriched")
```

- [ ] **Step 6: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(harvester): обогатитель (OCR, whisper, видео по запросу)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Матчер (эмбеддинги vault, классификация, полезность, калибровка)

**Files:**
- Create: `harvester/matcher.py`
- Test: `tests/test_matcher.py`

**Interfaces:**
- Consumes: `DB.update_post/labeled`, `files.drop_keep/list_sections`, `Settings.note_threshold/section_threshold/skip_sections/index_path/calibration_path`
- Produces:
  - `FastEmbedder(model: str, cache_dir: Path)`: `embed(texts: list[str]) -> np.ndarray` (строки нормированы)
  - `Match(score, target, section, section_score, top3: list[[rel, score]])`
  - `VaultVectors(s, embedder)`: `refresh() -> int` (сколько заметок пересчитано), `match(vec) -> Match`, атрибуты `embedder`, `sections: list[str]`
  - `usefulness(text) -> float`, `classify(m, note_t, section_t, sections: set[str]) -> (mode, target)` где mode `append | new | none`
  - `Matcher(s, db, vectors)`: `process(post) -> status` (`candidate | explore | video_ask`)
  - `best_threshold(pairs: list[(score, useful)]) -> float | None`, `calibrate(s, db) -> dict` (пишет `data/calibration.json` и сразу меняет `s`)

- [ ] **Step 1: Проверить модель эмбеддингов (вручную)**

```bash
cd "/Users/cucaramba/Claude Code/tg-harvester"
uv run python -c "
from fastembed import TextEmbedding
names = [m['model'] for m in TextEmbedding.list_supported_models()]
print('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2' in names)
print([n for n in names if 'multilingual' in n.lower()])"
```

Expected: `True`. Если `False`: выбери из напечатанного списка самую лёгкую мультиязычную модель, впиши её в `config/settings.yaml` (`embed_model`) и в HANDOFF «Отклонения». Если у выбранной модели e5-семейства нужен префикс `query: `/`passage: `, добавь его в `FastEmbedder.embed` и тоже запиши.

- [ ] **Step 2: Падающие тесты**

`tests/test_matcher.py`:

```python
import json
import os

import numpy as np

from harvester.matcher import Match, Matcher, VaultVectors, best_threshold, calibrate, classify, usefulness
from tests.helpers import CHAT_A

VOCAB = ["промпт", "claude", "бег", "колени", "гитара", "аккорды"]


class FakeEmbedder:
    """Мешок слов по маленькому словарю + отдельное измерение «ничего не знаю»."""

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        out = []
        for t in texts:
            v = np.array([t.lower().count(w) for w in VOCAB] + [0], dtype=np.float32)
            if not v.any():
                v[-1] = 1
            out.append(v / np.linalg.norm(v))
        return np.array(out)


def vault(s):
    files = {
        "ИИ работа/Промпты для Claude.md": "---\ntags: [x]\n---\n# Промпты\nпромпт claude промпт",
        "Фитнес и здоровье/Бег и колени.md": "бег колени бег",
        "Музыка/Аккорды.md": "гитара аккорды",
        "Музыка/Музыка — карта заметок.md": "гитара",
        "ИИ работа/Скиллы на машине/skill.md": "промпт",
        ".obsidian/x.md": "промпт",
    }
    for rel, text in files.items():
        p = s.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")
    for sec in ("ИИ работа", "Фитнес и здоровье", "Музыка"):
        (s.vault / f"{sec}.canvas").write_text('{"nodes":[],"edges":[]}', "utf-8")


def vectors(s):
    vault(s)
    v = VaultVectors(s, FakeEmbedder())
    v.refresh()
    return v


def test_refresh_embeds_only_changed_and_drops_deleted(s):
    vault(s)
    emb = FakeEmbedder()
    v = VaultVectors(s, emb)
    assert v.refresh() == 3
    assert sorted(v.rels) == ["ИИ работа/Промпты для Claude.md", "Музыка/Аккорды.md", "Фитнес и здоровье/Бег и колени.md"]
    v2 = VaultVectors(s, emb)
    assert v2.refresh() == 0  # кэш с диска
    p = s.vault / "Музыка/Аккорды.md"
    p.write_text("гитара аккорды гитара", "utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 10))
    assert v2.refresh() == 1
    (s.vault / "Фитнес и здоровье/Бег и колени.md").unlink()
    v2.refresh()
    assert "Фитнес и здоровье/Бег и колени.md" not in v2.rels
    assert v2.sections == ["ИИ работа", "Музыка", "Фитнес и здоровье"]


def test_match_finds_note_and_section(s):
    v = vectors(s)
    m = v.match(v.embedder.embed(["новый промпт для claude"])[0])
    assert m.target == "ИИ работа/Промпты для Claude.md" and m.section == "ИИ работа"
    assert m.score > 0.8 and m.top3[0][0] == m.target and len(m.top3) == 3


def test_classify():
    secs = {"ИИ работа"}
    assert classify(Match(0.7, "a.md", "ИИ работа", 0.7, []), 0.6, 0.45, secs) == ("append", "a.md")
    assert classify(Match(0.5, "a.md", "ИИ работа", 0.5, []), 0.6, 0.45, secs) == ("new", "ИИ работа")
    assert classify(Match(0.5, "a.md", "Чужой", 0.9, []), 0.6, 0.45, secs) == ("none", None)
    assert classify(Match(0.1, "a.md", "ИИ работа", 0.1, []), 0.6, 0.45, secs) == ("none", None)


def test_usefulness_prefers_guides_over_memes():
    guide = "Гайд: 5 бесплатных нейросетей\n1. первая https://a.io\n2. вторая https://b.io\n" + "x" * 300
    assert usefulness(guide) > usefulness("Шедевр") + 0.5


def _post(db, msg, text, kind=None):
    pid = db.add_post(CHAT_A, msg, None, "2026-10-01T00:00:00+00:00", text, kind)
    db.update_post(pid, status="enriched")
    return db.get_post(pid)


def test_matcher_statuses(s, db):
    v = vectors(s)
    m = Matcher(s, db, v)
    a = _post(db, 1, "промпт для claude, промпт")
    b = _post(db, 2, "смешной кот")
    c = _post(db, 3, "бег и колени: упражнение", "video")
    (s.keep_dir / str(b["id"])).mkdir(parents=True)
    assert m.process(a) == "candidate"
    row = db.get_post(a["id"])
    assert (row["mode"], row["target"]) == ("append", "ИИ работа/Промпты для Claude.md")
    assert json.loads(row["top3"])[0][0] == "ИИ работа/Промпты для Claude.md"
    assert m.process(b) == "explore" and db.get_post(b["id"])["mode"] is None
    assert not (s.keep_dir / str(b["id"])).exists()
    assert m.process(c) == "video_ask" and db.get_post(c["id"])["video_state"] == "ask"
    db.update_post(c["id"], video_state="done")
    assert m.process(db.get_post(c["id"])) == "candidate"


def test_best_threshold():
    assert best_threshold([(0.9, True), (0.8, True), (0.5, False), (0.4, False)]) == 0.8
    assert best_threshold([(0.9, True)]) is None
    assert best_threshold([(0.9, False)]) is None


def test_calibrate_writes_json_and_updates_settings(s, db):
    assert calibrate(s, db) == {}
    for i, (score, useful) in enumerate([(0.9, True), (0.8, True), (0.5, False), (0.4, False)], 1):
        pid = db.add_post(CHAT_A, i, None, "d", "t", None)
        db.update_post(pid, score=score, section_score=score - 0.1)
        db.add_label(pid, useful)
    res = calibrate(s, db)
    assert res == {"note_threshold": 0.8, "section_threshold": 0.7}
    assert json.loads(s.calibration_path.read_text("utf-8")) == res
    assert s.note_threshold == 0.8
```

- [ ] **Step 3: Убедиться, что падают**

Run: `uv run pytest tests/test_matcher.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.matcher'`

- [ ] **Step 4: `harvester/matcher.py`**

```python
"""Совпадение поста с vault без токенов: эмбеддинги заметок, центроиды разделов, полезность, калибровка."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import Settings
from .db import DB
from .files import drop_keep, list_sections

KEYWORDS = ("гайд", "инструкц", "промпт", "prompt", "инструмент", "сервис", "схем", "чек-лист", "шаблон",
            "бесплатн", "нейросет", "книг", "курс", "лайфхак", "как сделать", "github")
LIST_RE = re.compile(r"(?m)^\s*(\d+[.)]|[-•▪✅🔹])\s")


def _norm(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-9)


class FastEmbedder:
    def __init__(self, model: str, cache_dir: Path):
        from fastembed import TextEmbedding
        self.m = TextEmbedding(model_name=model, cache_dir=str(cache_dir))

    def embed(self, texts: list[str]) -> np.ndarray:
        vs = np.array(list(self.m.embed(texts)), dtype=np.float32)
        return vs / np.maximum(np.linalg.norm(vs, axis=1, keepdims=True), 1e-9)


@dataclass
class Match:
    score: float
    target: str | None
    section: str | None
    section_score: float
    top3: list


def note_text(path: Path) -> str:
    text = path.read_text("utf-8", errors="ignore")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:]
    return f"{path.stem}\n{text.strip()[:1000]}"


def section_of(rel: str) -> str | None:
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 else None


class VaultVectors:
    def __init__(self, s: Settings, embedder):
        self.s, self.embedder = s, embedder
        self.rels: list[str] = []
        self.vecs = np.zeros((0, 1), np.float32)
        self.centroids: dict[str, np.ndarray] = {}
        self.sections: list[str] = []

    def _files(self) -> dict[str, float]:
        out = {}
        for p in self.s.vault.rglob("*.md"):
            rel = p.relative_to(self.s.vault)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if any(part in self.s.skip_sections for part in rel.parts[:-1]) or p.stem.endswith("карта заметок"):
                continue
            out[rel.as_posix()] = p.stat().st_mtime
        return out

    def _load(self) -> dict[str, tuple[float, np.ndarray]]:
        try:
            data = np.load(self.s.index_path)
            meta = json.loads(str(data["meta"]))
            return {r: (mt, data["vecs"][i]) for i, (r, mt) in enumerate(meta)}
        except (OSError, ValueError, KeyError):
            return {}

    def _save(self, cache: dict) -> None:
        if not cache:
            self.s.index_path.unlink(missing_ok=True)
            return
        rels = sorted(cache)
        self.s.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.s.index_path.with_suffix(".tmp.npz")
        np.savez(tmp, vecs=np.stack([cache[r][1] for r in rels]),
                 meta=np.array(json.dumps([[r, cache[r][0]] for r in rels], ensure_ascii=False)))
        os.replace(tmp, self.s.index_path)

    def refresh(self) -> int:
        files = self._files()
        cache = self._load()
        todo = [r for r, mt in files.items() if r not in cache or cache[r][0] != mt]
        if todo:
            vecs = self.embedder.embed([note_text(self.s.vault / r) for r in todo])
            for r, v in zip(todo, vecs):
                cache[r] = (files[r], np.asarray(v, dtype=np.float32))
        cache = {r: cache[r] for r in files}
        self._save(cache)
        self.rels = sorted(cache)
        self.vecs = np.stack([cache[r][1] for r in self.rels]) if self.rels else np.zeros((0, 1), np.float32)
        groups: dict[str, list[int]] = {}
        for i, r in enumerate(self.rels):
            if sec := section_of(r):
                groups.setdefault(sec, []).append(i)
        self.centroids = {sec: _norm(self.vecs[idx].mean(axis=0)) for sec, idx in groups.items()}
        self.sections = list_sections(self.s.vault)
        return len(todo)

    def match(self, vec: np.ndarray) -> Match:
        if not self.rels:
            return Match(0.0, None, None, 0.0, [])
        sims = self.vecs @ vec
        order = np.argsort(-sims)[:3]
        top3 = [[self.rels[i], round(float(sims[i]), 4)] for i in order]
        sec, ss = max(((k, float(c @ vec)) for k, c in self.centroids.items()), key=lambda x: x[1], default=(None, 0.0))
        return Match(top3[0][1], top3[0][0], sec, round(ss, 4), top3)


def usefulness(text: str) -> float:
    t = text.lower()
    score = min(len(text), 2000) / 2000
    score += 0.3 * min(t.count("http"), 3) / 3
    score += 0.3 * (1.0 if LIST_RE.search(text) else 0.0)
    score += 0.4 * min(sum(k in t for k in KEYWORDS), 3) / 3
    return round(score, 3)


def classify(m: Match, note_t: float, section_t: float, sections: set[str]) -> tuple[str, str | None]:
    if m.target and m.score >= note_t:
        return "append", m.target
    if m.section in sections and m.section_score >= section_t:
        return "new", m.section
    return "none", None


class Matcher:
    def __init__(self, s: Settings, db: DB, vectors: VaultVectors):
        self.s, self.db, self.v = s, db, vectors

    def process(self, post) -> str:
        text = "\n".join(x for x in (post["text"], post["extra_text"]) if x).strip()
        m = self.v.match(self.v.embedder.embed([text[:2000]])[0])
        mode, target = classify(m, self.s.note_threshold, self.s.section_threshold, set(self.v.sections))
        has_video = any(x["kind"] == "video" for x in json.loads(post["media"]))
        if mode == "none":
            status = "explore"
        elif has_video and post["video_state"] == "none":
            status = "video_ask"
        else:
            status = "candidate"
        fields = dict(status=status, score=m.score, section_score=m.section_score, top3=m.top3,
                      usefulness=usefulness(text), mode=None if mode == "none" else mode, target=target)
        if status == "video_ask":
            fields["video_state"] = "ask"
        self.db.update_post(post["id"], **fields)
        if status == "explore":
            drop_keep(self.s, post["id"])  # вне тем картинки не храним, иначе диск забьют мемы
        return status


def best_threshold(pairs: list[tuple[float, bool]]) -> float | None:
    """Порог с лучшим F1 по разметке Некита. При равенстве берём более строгий."""
    if not any(u for _, u in pairs) or all(u for _, u in pairs):
        return None
    best, best_f1 = None, -1.0
    for t in sorted({round(x, 3) for x, _ in pairs}):
        tp = sum(1 for x, u in pairs if u and x >= t)
        fp = sum(1 for x, u in pairs if not u and x >= t)
        fn = sum(1 for x, u in pairs if u and x < t)
        f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        if f1 >= best_f1:
            best, best_f1 = t, f1
    return best


def calibrate(s: Settings, db: DB) -> dict:
    rows = db.labeled()
    res = {}
    for key, col in (("note_threshold", "score"), ("section_threshold", "section_score")):
        t = best_threshold([(r[col], bool(r["useful"])) for r in rows if r[col] is not None])
        if t is not None:
            res[key] = t
    if res:
        p = s.calibration_path
        p.parent.mkdir(parents=True, exist_ok=True)
        old = json.loads(p.read_text("utf-8")) if p.exists() else {}
        p.write_text(json.dumps({**old, **res}, ensure_ascii=False), "utf-8")
        for k, v in res.items():
            setattr(s, k, v)
    return res
```

- [ ] **Step 5: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(harvester): матчер на эмбеддингах и калибровка порогов

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Мозг (ночные батчи в claude -p, бюджет, валидация)

**Files:**
- Create: `harvester/brain.py`
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `DB.night_queue/retro_active/add_draft/update_post/bump_attempts/push_alert/kv_set`, `files.drop_keep/list_sections`, `FakeClaude` (тесты)
- Produces:
  - `BrainError`, `run_claude(s, prompt, *, system, schema=None, tools=(), add_dirs=(), timeout) -> dict | str` (копия idea-bot)
  - `chunks(xs, n)`, `demote(md)`, `is_limit(err) -> bool`
  - `Verdict(post_id, mode, target, title, content, reason, keep_image)`, `validate(items, posts, sections, vault) -> dict[int, Verdict]`
  - `alternatives(post, sections, vault) -> list[[mode, target]]`
  - `run_night(s, db, claude=run_claude) -> dict` (отчёт также в `kv["night_report"]`)

Бюджет: `limit = max_calls_per_night * (retro_multiplier если db.retro_active() иначе 1)`. Ретрай считается отдельным вызовом. Ошибка лимита подписки (`limit`, `usage`, `лимит` в тексте) или незапускающийся `claude` обрывает ночь, посты остаются `candidate` без штрафа.

- [ ] **Step 1: Падающие тесты**

`tests/test_brain.py`:

```python
import asyncio
import json

from harvester.brain import BrainError, run_night
from tests.helpers import CHAT_A, FakeClaude

REL = "ИИ работа/Промпты.md"


def setup_vault(s):
    for sec in ("ИИ работа", "Музыка"):
        (s.vault / sec).mkdir()
        (s.vault / f"{sec}.canvas").write_text('{"nodes":[],"edges":[]}', "utf-8")
    (s.vault / REL).write_text("# Промпты\n", "utf-8")


def cand(db, msg, *, source="history", score=0.7, status="candidate", extra="", images=(), usefulness=0.5):
    pid = db.add_post(CHAT_A, msg, None, "2026-10-01T00:00:00+00:00", f"пост {msg} про промпты", None, source)
    db.update_post(pid, status=status, score=score, extra_text=extra, images=list(images), top3=[[REL, 0.7]],
                   mode="append", target=REL, usefulness=usefulness)
    return pid


def answer(*items):
    return {"items": list(items)}


def item(pid, mode="append", target=REL, title=None, content="суть", reason="ок", keep=False):
    return {"post_id": pid, "mode": mode, "target": target, "title": title, "content": content,
            "reason": reason, "keep_image": keep}


def night(s, db, fake):
    return asyncio.run(run_night(s, db, claude=fake))


def test_drafts_skips_and_missing(s, db):
    setup_vault(s)
    a, b, c = cand(db, 1), cand(db, 2), cand(db, 3)
    fake = FakeClaude([lambda p: answer(item(a), item(b, mode="skip", reason="новость")), answer()])
    rep = night(s, db, fake)
    assert db.get_post(a)["status"] == "drafted"
    assert db.get_post(b)["status"] == "rejected" and "новость" in db.get_post(b)["reason"]
    assert db.get_post(c)["status"] == "candidate" and db.get_post(c)["attempts"] == 1
    d = db.drafts("drafted")[0]
    assert (d["mode"], d["target"], d["content"]) == ("append", REL, "суть")
    alts = json.loads(d["alts"])
    assert ["append", REL] in alts and ["new", "ИИ работа"] in alts
    assert (rep["drafted"], rep["skipped"], rep["calls"]) == (1, 1, 2)
    assert json.loads(db.kv_get("night_report"))["drafted"] == 1


def test_call_limit_and_retro_multiplier(s, db):
    setup_vault(s)
    for i in range(1, 11):
        cand(db, i)
    fake = FakeClaude([answer()] * 10)
    rep = night(s, db, fake)
    assert rep["limit"] == 3 and len(fake.calls) == 3
    db.save_channel(-1, read_mark=0, new_cursor=0, retro_cursor=1, retro_until=None, phase="retro")
    fake2 = FakeClaude([answer()] * 10)
    rep2 = night(s, db, fake2)
    assert rep2["limit"] == 6 and len(fake2.calls) == 5  # 10 постов по 2 = 5 батчей, в лимит влезли


def test_usage_limit_aborts_night_without_penalty(s, db):
    setup_vault(s)
    a = cand(db, 1)
    fake = FakeClaude([BrainError("claude: You've hit your usage limit")])
    rep = night(s, db, fake)
    assert rep["aborted"] and len(fake.calls) == 1
    p = db.get_post(a)
    assert (p["status"], p["attempts"]) == ("candidate", 0)
    assert any("Ночь прервана" in t for t in db.pop_alerts())


def test_missing_claude_binary_aborts_night(s, db):
    setup_vault(s)
    a = cand(db, 1)
    rep = night(s, db, FakeClaude([BrainError("не смог запустить claude: No such file")]))
    assert rep["aborted"] and db.get_post(a)["attempts"] == 0


def test_other_error_is_retried_once(s, db):
    setup_vault(s)
    a = cand(db, 1)
    fake = FakeClaude([BrainError("claude не ответил за 10 с"), lambda p: answer(item(a))])
    night(s, db, fake)
    assert db.get_post(a)["status"] == "drafted" and len(fake.calls) == 2


def test_three_failed_nights_mark_error(s, db):
    setup_vault(s)
    a = cand(db, 1)
    for _ in range(3):
        night(s, db, FakeClaude([BrainError("сбой"), BrainError("сбой")]))
    assert db.get_post(a)["status"] == "error"


def test_bad_targets_fall_back_to_section(s, db):
    setup_vault(s)
    a, b = cand(db, 1), cand(db, 2)
    fake = FakeClaude([lambda p: answer(item(a, target="ИИ работа/Нет такой.md"),
                                        item(b, mode="new", target="../../etc", title="Заметка"))])
    night(s, db, fake)
    da, dbb = sorted(db.drafts("drafted"), key=lambda d: d["post_id"])
    assert (da["mode"], da["target"], da["title"]) == ("new", "ИИ работа", "суть")
    assert (dbb["mode"], dbb["target"], dbb["title"]) == ("new", "ИИ работа", "Заметка")


def test_live_first_and_explore_quota(s, db):
    setup_vault(s)
    old = cand(db, 1, score=0.9)
    live = cand(db, 2, score=0.65, source="live")
    e1 = cand(db, 3, status="explore", usefulness=0.9)
    e2 = cand(db, 4, status="explore", usefulness=0.8)
    e3 = cand(db, 5, status="explore", usefulness=0.1)
    fake = FakeClaude([answer()] * 5)
    night(s, db, fake)
    prompts = "\n".join(c[0] for c in fake.calls)
    assert prompts.index(f"post_id {live} ") < prompts.index(f"post_id {old} ")
    assert f"post_id {e1} " in prompts and f"post_id {e2} " in prompts and f"post_id {e3} " not in prompts


def test_images_only_without_ocr_and_capped(s, db, tmp_path):
    setup_vault(s)
    img1, img2 = tmp_path / "k1" / "a.jpg", tmp_path / "k2" / "b.jpg"
    for p in (img1, img2):
        p.parent.mkdir()
        p.write_bytes(b"x")
    cand(db, 1, images=[str(img1)])
    cand(db, 2, images=[str(img2)])
    cand(db, 3, images=[str(img1)], extra="текст с картинки")
    fake = FakeClaude([answer()] * 3)
    night(s, db, fake)
    prompt, kw = fake.calls[0]
    assert kw["tools"] == ("Read",) and str(img1) in prompt and str(img2) not in prompt
    assert kw["add_dirs"] == (str(img1.parent),)
    assert fake.calls[1][1]["tools"] == ()
```

- [ ] **Step 2: Убедиться, что падают**

Run: `uv run pytest tests/test_brain.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.brain'`

- [ ] **Step 3: `harvester/brain.py`**

```python
"""Ночной разбор кандидатов через claude -p с жёстким лимитом вызовов."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import Settings
from .db import DB
from .files import drop_keep, list_sections


class BrainError(RuntimeError):
    pass


SYSTEM = """Ты отбираешь посты из Telegram-каналов для Obsidian Некита. Его vault это список «изучить и использовать позже», разложенный по темам. Отвечай по-русски, строго по JSON-схеме, по одному элементу на каждый post_id.

Ценно: практическая польза потом. Гайд, инструкция, промпт, инструмент или сервис со ссылкой, схема заработка, цифры и выводы, рекомендация книги или курса, упражнение или полезная привычка.
Не ценно: новости, мемы, реклама, мотивационные цитаты без сути, анонсы, розыгрыши. Для них mode skip.

mode:
- append: дописать в существующую заметку. target: точный путь из «Похожие заметки» этого поста. Только если пост прямо по теме этой заметки.
- new: новая заметка. target: точное имя раздела из списка разделов. title: содержательный заголовок до 80 символов.
- skip: не ценно, reason обязателен.

content: что записать. Для append одна-три строки сути. Для new тело заметки в markdown без заголовка верхнего уровня. Промпты и команды переноси дословно в блоке кода. Ссылки из поста сохраняй. Не выдумывай ничего, чего нет в посте. Не используй длинное тире, пиши через запятую, двоеточие или дефис.
keep_image: true, только если картинка поста сама несёт суть (схема, скрин интерфейса, таблица).
Цели Некита даны, чтобы понимать, что ему важно. Полезный для целей пост вне текущих тем бери как new в ближайший по смыслу раздел."""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "post_id": {"type": "integer"},
            "mode": {"type": "string", "enum": ["append", "new", "skip"]},
            "target": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "content": {"type": "string"},
            "reason": {"type": "string"},
            "keep_image": {"type": "boolean"},
        },
        "required": ["post_id", "mode", "target", "title", "content", "reason", "keep_image"],
    }}},
    "required": ["items"],
}


async def run_claude(s: Settings, prompt: str, *, system: str, schema: dict | None = None,
                     tools: tuple[str, ...] = (), add_dirs: tuple[str, ...] = (), timeout: int) -> dict | str:
    args = [s.claude_bin, "-p", "--output-format", "json", "--no-session-persistence",
            "--system-prompt", system, "--tools", ",".join(tools)]
    if tools:
        args += ["--allowedTools", ",".join(tools)]  # без этого в -p тул упрётся в запрос разрешения
    for d in add_dirs:
        args += ["--add-dir", d]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    s.data_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": f"{os.path.dirname(s.claude_bin)}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(s.data_dir), env=env)
    except OSError as e:
        raise BrainError(f"не смог запустить claude: {e}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BrainError(f"claude не ответил за {timeout} с")
    if proc.returncode != 0:
        raise BrainError(f"claude exit {proc.returncode}: {(err or out).decode(errors='ignore')[-300:]}")
    try:
        data = json.loads(out)
    except ValueError:
        raise BrainError("claude вернул не JSON")
    if data.get("is_error"):
        raise BrainError(f"claude: {str(data.get('result'))[:300]}")
    if schema is not None:
        so = data.get("structured_output")
        if not isinstance(so, dict):
            raise BrainError("claude не вернул structured_output")
        return so
    return str(data.get("result") or "")


@dataclass
class Verdict:
    post_id: int
    mode: str
    target: str | None
    title: str | None
    content: str
    reason: str
    keep_image: bool


def chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def demote(md: str) -> str:
    """Заголовки # и ## из ответа -> ###: иначе сломают структуру заметки."""
    return re.sub(r"(?m)^#{1,2} ", "### ", md)


def is_limit(e: Exception) -> bool:
    """Лимит подписки или claude вообще не запускается: ночь обрывается, посты ждут без штрафа."""
    t = str(e).lower()
    return any(w in t for w in ("limit", "лимит", "usage", "не смог запустить"))


def _safe_note(vault: Path, rel: str | None) -> bool:
    if not rel:
        return False
    p = (vault / rel).resolve()
    return p.is_relative_to(vault.resolve()) and p.is_file()


def _top_sections(post) -> list[str]:
    return [r.split("/")[0] for r, _ in json.loads(post["top3"]) if "/" in r]


def fallback_section(post, sections: list[str]) -> str | None:
    cands = ([post["target"]] if post["mode"] == "new" and post["target"] else []) + _top_sections(post)
    return next((c for c in cands if c in sections), None)


def alternatives(post, sections: list[str], vault: Path) -> list[list[str]]:
    alts = [["append", r] for r, _ in json.loads(post["top3"]) if _safe_note(vault, r)]
    extra = _top_sections(post) + ([post["target"]] if post["mode"] == "new" and post["target"] else [])
    for sec in extra:
        if sec in sections and ["new", sec] not in alts:
            alts.append(["new", sec])
    return alts[:6]


def validate(items: list[dict], posts: list, sections: list[str], vault: Path) -> dict[int, Verdict]:
    by_id = {p["id"]: p for p in posts}
    out: dict[int, Verdict] = {}
    for it in items:
        pid = it.get("post_id")
        if pid not in by_id or pid in out:
            continue
        p = by_id[pid]
        content = demote(str(it.get("content") or "").strip())
        reason = str(it.get("reason") or "").strip()
        keep = bool(it.get("keep_image"))
        mode, target = it.get("mode"), it.get("target")
        if mode not in ("append", "new") or not content:
            out[pid] = Verdict(pid, "skip", None, None, "", reason or "не ценно", False)
            continue
        allowed = [r for r, _ in json.loads(p["top3"])]
        if mode == "append" and target in allowed and _safe_note(vault, target):
            out[pid] = Verdict(pid, "append", target, None, content, reason, keep)
            continue
        if not (mode == "new" and target in sections):
            target = fallback_section(p, sections)
        if target is None:
            out[pid] = Verdict(pid, "skip", None, None, "", "некуда положить", False)
            continue
        title = (str(it.get("title") or "").strip() or content.splitlines()[0]).lstrip("#-* ").strip()[:80]
        out[pid] = Verdict(pid, "new", target, title, content, reason, keep)
    return out


def build_prompt(posts: list, sections: list[str], goals: str, channel_name, images: list[tuple[int, str]]) -> str:
    parts = ["## Разделы vault", ", ".join(sections)]
    if goals:
        parts += ["", "## Цели Некита (кратко)", goals]
    parts += ["", "## Посты"]
    for p in posts:
        top = "\n".join(f"  - {r} ({sc})" for r, sc in json.loads(p["top3"])) or "  (нет)"
        text = p["text"] or ""
        if p["extra_text"]:
            text += "\n[текст с картинок и аудио]\n" + p["extra_text"]
        parts += [f"### post_id {p['id']} · {channel_name(p['chat_id'])} · {p['date'][:10]}",
                  f"Похожие заметки:\n{top}", text.strip()[:4000], ""]
    if images:
        parts += ["## Картинки (посмотри тулом Read)", *[f"post_id {pid}: {path}" for pid, path in images]]
    return "\n".join(parts)


async def run_night(s: Settings, db: DB, claude=run_claude) -> dict:
    retro = db.retro_active()
    limit = int(s.max_calls_per_night * (s.retro_multiplier if retro else 1))
    queue = db.night_queue(s.explore_quota)
    sections = list_sections(s.vault)
    goals_p = s.vault / s.goals_note
    goals = goals_p.read_text("utf-8")[:3000] if goals_p.exists() else ""
    rep = {"date": date.today().isoformat(), "limit": limit, "calls": 0, "queue": len(queue), "drafted": 0,
           "skipped": 0, "errors": 0, "aborted": None, "retro": retro}
    images_left = s.max_images_per_night
    for batch in chunks(queue, s.batch_size):
        if rep["calls"] >= limit:
            break
        imgs: list[tuple[int, str]] = []
        for p in batch:
            if images_left and not (p["extra_text"] or "").strip():
                path = next((x for x in json.loads(p["images"]) if Path(x).exists()), None)
                if path:
                    imgs.append((p["id"], path))
                    images_left -= 1
        prompt = build_prompt(batch, sections, goals, s.channel_name, imgs)
        kw = dict(system=SYSTEM, schema=SCHEMA, tools=("Read",) if imgs else (),
                  add_dirs=tuple(sorted({str(Path(x).parent) for _, x in imgs})), timeout=s.claude_timeout)
        raw = None
        for _ in range(2):
            if rep["calls"] >= limit:
                break
            rep["calls"] += 1
            try:
                raw = await claude(s, prompt, **kw)
                break
            except BrainError as e:
                if is_limit(e):
                    rep["aborted"] = str(e)[:200]
                    break
        if rep["aborted"]:
            break
        verdicts = validate(raw.get("items") or [], batch, sections, s.vault) if isinstance(raw, dict) else {}
        for p in batch:
            v = verdicts.get(p["id"])
            if v is None:
                if db.bump_attempts(p["id"]) >= 3:
                    db.update_post(p["id"], status="error", reason="claude: нет ответа по посту")
                    drop_keep(s, p["id"])
                    rep["errors"] += 1
            elif v.mode == "skip":
                db.update_post(p["id"], status="rejected", reason=f"ai: {v.reason}"[:300])
                drop_keep(s, p["id"])
                rep["skipped"] += 1
            else:
                db.add_draft(p["id"], v.mode, v.target, v.title, v.content, v.keep_image,
                             alternatives(p, sections, s.vault))
                db.update_post(p["id"], status="drafted")
                rep["drafted"] += 1
    if rep["aborted"]:
        db.push_alert(f"⛔ Ночь прервана: {rep['aborted']}")
    db.kv_set("night_report", json.dumps(rep, ensure_ascii=False))
    return rep
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(harvester): ночной мозг с лимитом вызовов

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Карточки и бот (ревью, видео, пилот, откат, отчёты, алерты)

**Files:**
- Create: `harvester/cards.py`, `harvester/bot.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `Writer.apply/rollback`, `WriteError`, `post_link`, `DB.*`, `calibrate` (задача 6), `make_draft`
- Produces:
  - `cards.py`: `draft_card(d, post, channel) -> str`, `draft_kb(did, link)`, `alts_kb(did, alts)`, `written_kb(bid)`, `confirm_kb(bid)`, `video_card(post, channel)`, `video_kb(pid)`, `pilot_card(post, channel)`, `pilot_kb(pid)`, `audit_card(audit, block)`, `audit_kb(aid)`, `night_text(r)`, `audit_text(r)`, `status_text(counts, phases)`
  - `bot.App(s, db, writer, now=datetime.now, pause=1.1)`: `tick(bot)`, `handle_callback(data) -> (text | None, kb | None)`, `pilot(bot, n=50)`, `status() -> str`
  - `bot.build_dispatcher(app) -> aiogram.Dispatcher`
  - Callback-данные: `ok:D`, `no:D`, `alt:D`, `back:D`, `to:D:I`, `rb:B`, `rby:B`, `rbn:B`, `vy:P`, `vn:P`, `pl:P:1|0`, `ab:A`, `ak:A`

Отправка карточек, видео-вопросов, отчётов и аудита начинается с `cards_hour` (9:00), чтобы бот не будил ночью. Алерты шлются сразу.

- [ ] **Step 1: Падающие тесты**

`tests/test_bot.py`:

```python
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from harvester.bot import App
from harvester.cards import draft_card
from harvester.writer import Writer
from tests.helpers import CHAT_A, make_draft

REL = "ИИ работа/Промпты.md"
NOTE = "# Промпты\n"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(message_id=len(self.sent))


def app_at(s, db, hour=10):
    return App(s, db, Writer(s, db), now=lambda: datetime(2026, 10, 11, hour, 0), pause=0)


def note(s):
    p = s.vault / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(NOTE, "utf-8")
    return p


def cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def test_draft_card_texts(s, db):
    did = make_draft(db, 1, "append", REL, content="x" * 5000)
    d = db.get_draft(did)
    text = draft_card(d, db.get_post(d["post_id"]), "Канал А")
    assert text.startswith("📌 Канал А · 2026-10-01\n→ дописать в [[Промпты]]")
    assert len(text) <= 3500
    db.update_draft(did, mode="new", target="Музыка", title="Сведение")
    assert "→ новая заметка в «Музыка»: Сведение" in draft_card(db.get_draft(did), db.get_post(d["post_id"]), "К")
    assert "—" not in text


def test_cards_wait_for_hour_and_respect_daily_cap(s, db):
    for i in (1, 2, 3):
        make_draft(db, i, "append", REL)
    bot = FakeBot()
    asyncio.run(app_at(s, db, hour=8).tick(bot))
    assert bot.sent == []
    app = app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 2
    assert [d["status"] for d in db.drafts("sent")] == ["sent", "sent"]
    assert cbs(bot.sent[0][2]["reply_markup"])[:3] == ["ok:1", "no:1", "alt:1"]


def test_ok_writes_then_rollback_restores(s, db):
    p = note(s)
    did = make_draft(db, 1, "append", REL)
    app = app_at(s, db)
    text, kb = app.handle_callback(f"ok:{did}")
    assert "✅ Записано в [[Промпты]]" in text
    rb = cbs(kb)[0]
    assert rb.startswith("rb:")
    bid = rb.split(":")[1]
    text2, kb2 = app.handle_callback(f"rb:{bid}")
    assert text2 is None and cbs(kb2) == [f"rby:{bid}", f"rbn:{bid}"]
    text3, _ = app.handle_callback(f"rby:{bid}")
    assert text3.startswith("↩️ Откатил") and p.read_text("utf-8") == NOTE


def test_ok_on_missing_note_reports_error(s, db):
    did = make_draft(db, 1, "append", "ИИ работа/Нет.md")
    text, kb = app_at(s, db).handle_callback(f"ok:{did}")
    assert "⚠️ Не записал" in text and kb is None


def test_no_rejects_draft_and_post(s, db):
    did = make_draft(db, 1, "append", REL)
    text, _ = app_at(s, db).handle_callback(f"no:{did}")
    assert "❌ Отклонено" in text
    d = db.get_draft(did)
    assert d["status"] == "rejected" and db.get_post(d["post_id"])["status"] == "rejected"


def test_alt_and_to_change_target(s, db):
    did = make_draft(db, 1, "append", REL)
    db.update_draft(did, alts=[["append", REL], ["new", "Музыка"]])
    app = app_at(s, db)
    _, kb = app.handle_callback(f"alt:{did}")
    assert cbs(kb) == [f"to:{did}:0", f"to:{did}:1", f"back:{did}"]
    text, kb2 = app.handle_callback(f"to:{did}:1")
    d = db.get_draft(did)
    assert (d["mode"], d["target"], d["title"]) == ("new", "Музыка", "суть поста")
    assert "новая заметка в «Музыка»" in text and cbs(kb2)[0] == f"ok:{did}"


def test_video_buttons(s, db):
    pid = db.add_post(CHAT_A, 1, None, "2026-10-01T00:00:00+00:00", "видео про промпты", "video")
    db.update_post(pid, status="video_ask", video_state="ask")
    bot = FakeBot()
    app = app_at(s, db)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    assert len(bot.sent) == 1 and cbs(bot.sent[0][2]["reply_markup"]) == [f"vy:{pid}", f"vn:{pid}"]
    app.handle_callback(f"vy:{pid}")
    assert db.get_post(pid)["video_state"] == "yes"
    pid2 = db.add_post(CHAT_A, 2, None, "d", "ещё видео", "video")
    db.update_post(pid2, status="video_ask", video_state="asked")
    app.handle_callback(f"vn:{pid2}")
    assert db.get_post(pid2)["status"] == "rejected"


def test_pilot_and_labels(s, db):
    for i in (1, 2):
        pid = db.add_post(CHAT_A, i, None, "2026-10-01T00:00:00+00:00", f"пост {i}", None)
        db.update_post(pid, score=0.5, section_score=0.4)
    bot = FakeBot()
    app = app_at(s, db)
    asyncio.run(app.pilot(bot, n=5))
    assert len(bot.sent) == 2
    app.handle_callback("pl:1:1")
    app.handle_callback("pl:2:0")
    assert sorted(r["useful"] for r in db.labeled()) == [0, 1]


def test_alerts_now_reports_once_after_hour(s, db):
    db.push_alert("💾 мало места")
    rep = {"date": "2026-10-11", "limit": 5, "calls": 2, "queue": 7, "drafted": 3, "skipped": 4, "errors": 0,
           "aborted": None, "retro": True}
    db.kv_set("night_report", json.dumps(rep))
    bot = FakeBot()
    asyncio.run(app_at(s, db, hour=4).tick(bot))
    assert [t for _, t, _ in bot.sent] == ["💾 мало места"]
    app = app_at(s, db, hour=10)
    asyncio.run(app.tick(bot))
    asyncio.run(app.tick(bot))
    nights = [t for _, t, _ in bot.sent if t.startswith("🌙")]
    assert len(nights) == 1 and "2/5" in nights[0]


def test_audit_card_and_rollback(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    aid = db.add_audit(bid, "doubt", "не по теме")
    bot = FakeBot()
    app = App(s, db, w, now=lambda: datetime(2026, 10, 11, 10), pause=0)
    asyncio.run(app.tick(bot))
    audit_msgs = [x for x in bot.sent if x[1].startswith("🔍")]
    assert len(audit_msgs) == 1 and cbs(audit_msgs[0][2]["reply_markup"]) == [f"ab:{aid}", f"ak:{aid}"]
    text, _ = app.handle_callback(f"ab:{aid}")
    assert text.startswith("↩️") and p.read_text("utf-8") == NOTE
    assert db.get_audit(aid)["status"] == "rolled_back"


def test_status_text(s, db):
    db.save_channel(CHAT_A, read_mark=1, new_cursor=1, retro_cursor=2, retro_until=None, phase="retro")
    db.add_post(CHAT_A, 1, None, "d", "t", None)
    text = app_at(s, db).status()
    assert "Канал А: ретро" in text and "new: 1" in text
```

- [ ] **Step 2: Убедиться, что падают**

Run: `uv run pytest tests/test_bot.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.bot'`

- [ ] **Step 3: `harvester/cards.py`**

```python
"""Тексты и клавиатуры бота. Чистые функции без сети."""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as K

MAX = 3500
PHASES = {"new": "новое", "retro": "ретро", "done": "готово"}


def _trim(t: str, n: int = MAX) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


def _stem(rel: str) -> str:
    return PurePosixPath(rel).stem


def where(mode: str, target: str, title: str | None = None) -> str:
    if mode == "append":
        return f"→ дописать в [[{_stem(target)}]]"
    return f"→ новая заметка в «{target}»" + (f": {title}" if title else "")


def draft_card(d, post, channel: str) -> str:
    return _trim(f"📌 {channel} · {post['date'][:10]}\n{where(d['mode'], d['target'], d['title'])}\n\n{d['content']}")


def draft_kb(did: int, link: str) -> K:
    return K(inline_keyboard=[
        [B(text="✅", callback_data=f"ok:{did}"), B(text="❌", callback_data=f"no:{did}"),
         B(text="✏️ другая", callback_data=f"alt:{did}")],
        [B(text="🔗 пост", url=link)],
    ])


def alts_kb(did: int, alts: list) -> K:
    rows = [[B(text=_trim(f"дописать: {_stem(t)}" if m == "append" else f"новая в «{t}»", 60),
               callback_data=f"to:{did}:{i}")] for i, (m, t) in enumerate(alts)]
    rows.append([B(text="↩️ назад", callback_data=f"back:{did}")])
    return K(inline_keyboard=rows)


def written_kb(bid: int) -> K:
    return K(inline_keyboard=[[B(text="↩️ Откатить", callback_data=f"rb:{bid}")]])


def confirm_kb(bid: int) -> K:
    return K(inline_keyboard=[[B(text="Да, откатить", callback_data=f"rby:{bid}"),
                               B(text="Отмена", callback_data=f"rbn:{bid}")]])


def video_card(post, channel: str) -> str:
    return _trim(f"🎬 {channel} · {post['date'][:10]}\nВидео-пост прошёл фильтр. Разобрать видео "
                 f"(скачать, расшифровать звук, прочитать кадры)?\n\n{post['text']}", 1500)


def video_kb(pid: int) -> K:
    return K(inline_keyboard=[[B(text="🎬 Разобрать", callback_data=f"vy:{pid}"),
                               B(text="❌ Не надо", callback_data=f"vn:{pid}")]])


def pilot_card(post, channel: str) -> str:
    body = "\n".join(x for x in (post["text"], post["extra_text"]) if x)
    return _trim(f"🧪 {channel} · совпадение {post['score']:.2f}\n\n{body}", 1500)


def pilot_kb(pid: int) -> K:
    return K(inline_keyboard=[[B(text="👍 полезно", callback_data=f"pl:{pid}:1"),
                               B(text="👎 мусор", callback_data=f"pl:{pid}:0")]])


def audit_card(audit, block) -> str:
    extra = json.loads(block["extra"])
    added = extra.get("line") or extra.get("item") or ""
    return _trim(f"🔍 Аудит: сомнительный блок в «{block['path']}»\nПричина: {audit['reason']}\n\n{added}")


def audit_kb(aid: int) -> K:
    return K(inline_keyboard=[[B(text="↩️ Откатить", callback_data=f"ab:{aid}"),
                               B(text="Оставить", callback_data=f"ak:{aid}")]])


def night_text(r: dict) -> str:
    text = (f"🌙 Ночь {r['date']}: вызовов Claude {r['calls']}/{r['limit']}, в очереди было {r['queue']}, "
            f"черновиков {r['drafted']}, отсеяно {r['skipped']}, ошибок {r['errors']}")
    if r.get("retro"):
        text += "\nИдёт догон архива, лимит повышен"
    if r.get("aborted"):
        text += f"\n⛔ Прервано: {r['aborted']}"
    return text


def audit_text(r: dict) -> str:
    text = f"🔍 Аудит {r['date']}: проверено {r['checked']}, сомнительных {r['doubt']}, вызовов {r['calls']}/{r['limit']}"
    return text + (f"\n⛔ Прервано: {r['aborted']}" if r.get("aborted") else "")


def status_text(counts: dict, phases: list[tuple[str, str]]) -> str:
    lines = ["📊 Посты: " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "пока пусто")]
    lines += [f"{name}: {PHASES.get(ph, ph)}" for name, ph in phases]
    return "\n".join(lines)
```

- [ ] **Step 4: `harvester/bot.py`**

```python
"""Бот ревью: карточки черновиков, видео-вопросы, пилот, откат, отчёты. Пишет только владельцу."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import PurePosixPath

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from .cards import (alts_kb, audit_card, audit_kb, audit_text, confirm_kb, draft_card, draft_kb, night_text,
                    pilot_card, pilot_kb, status_text, video_card, video_kb, written_kb)
from .config import Settings
from .db import DB
from .files import drop_keep
from .matcher import calibrate
from .writer import WriteError, Writer, post_link

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class App:
    def __init__(self, s: Settings, db: DB, writer: Writer, now=datetime.now, pause: float = 1.1):
        self.s, self.db, self.writer, self.now, self.pause = s, db, writer, now, pause

    # --- исходящие
    async def _send(self, bot, text: str, kb=None):
        return await bot.send_message(self.s.owner_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)

    def _day_open(self) -> bool:
        return self.now().hour >= self.s.cards_hour

    async def tick(self, bot) -> None:
        for step in (self.send_alerts, self.send_reports, self.send_video_asks, self.send_cards, self.send_audits):
            try:
                await step(bot)
            except Exception:
                log.exception("tick %s", step.__name__)

    async def send_alerts(self, bot) -> None:
        for t in self.db.pop_alerts():
            await self._send(bot, t)

    async def send_reports(self, bot) -> None:
        if not self._day_open():
            return
        for key, render in (("night_report", night_text), ("audit_report", audit_text)):
            raw = self.db.kv_get(key)
            if raw and self.db.kv_get(key + "_sent") != raw:
                await self._send(bot, render(json.loads(raw)))
                self.db.kv_set(key + "_sent", raw)

    async def send_video_asks(self, bot) -> None:
        if not self._day_open():
            return
        for p in self.db.video_asks(10):
            await self._send(bot, video_card(p, self.s.channel_name(p["chat_id"])), video_kb(p["id"]))
            self.db.update_post(p["id"], video_state="asked")

    def _card(self, d):
        post = self.db.get_post(d["post_id"])
        return (draft_card(d, post, self.s.channel_name(post["chat_id"])),
                draft_kb(d["id"], post_link(post["chat_id"], post["msg_id"])))

    async def send_cards(self, bot) -> None:
        if not self._day_open():
            return
        key = f"cards:{self.now().date().isoformat()}"
        sent = int(self.db.kv_get(key, "0"))
        for d in self.db.drafts("drafted", max(0, self.s.cards_per_day - sent)):
            text, kb = self._card(d)
            msg = await self._send(bot, text, kb)
            self.db.update_draft(d["id"], status="sent", card_msg_id=msg.message_id,
                                 sent_on=self.now().date().isoformat())
            sent += 1
            self.db.kv_set(key, str(sent))

    async def send_audits(self, bot) -> None:
        if not self._day_open():
            return
        for a in self.db.audits("new"):
            b = self.db.get_block(a["block_id"])
            if b is None or b["rolled_back"]:
                self.db.update_audit(a["id"], status="kept")
                continue
            await self._send(bot, audit_card(a, b), audit_kb(a["id"]))
            self.db.update_audit(a["id"], status="sent")

    async def pilot(self, bot, n: int = 50) -> None:
        for p in self.db.pilot_sample(n):
            await self._send(bot, pilot_card(p, self.s.channel_name(p["chat_id"])), pilot_kb(p["id"]))
            await asyncio.sleep(self.pause)

    def status(self) -> str:
        phases = [(c.name, (self.db.channel(c.id) or {"phase": "ещё не начат"})["phase"]) for c in self.s.channels]
        return status_text(self.db.counts(), phases)

    # --- кнопки
    def handle_callback(self, data: str):
        kind, _, rest = data.partition(":")
        h = {"ok": self.on_ok, "no": self.on_no, "alt": self.on_alt, "back": self.on_back, "to": self.on_to,
             "rb": self.on_rb, "rby": self.on_rby, "rbn": self.on_rbn, "vy": self.on_vy, "vn": self.on_vn,
             "pl": self.on_pl, "ab": self.on_ab, "ak": self.on_ak}.get(kind)
        if h is None or not rest:
            return None, None
        try:
            return h(*map(int, rest.split(":")))
        except (ValueError, TypeError, IndexError):
            return None, None

    def on_ok(self, did: int):
        d = self.db.get_draft(did)
        if d is None:
            return "Черновик не найден", None
        text, _ = self._card(d)
        try:
            bid = self.writer.apply(did)
        except WriteError as e:
            return f"{text}\n\n⚠️ Не записал: {e}", None
        b = self.db.get_block(bid)
        return f"{text}\n\n✅ Записано в [[{PurePosixPath(b['path']).stem}]]", written_kb(bid)

    def on_no(self, did: int):
        d = self.db.get_draft(did)
        if d is None or d["status"] not in ("drafted", "sent"):
            return "Уже обработано", None
        text, _ = self._card(d)
        self.db.update_draft(did, status="rejected")
        self.db.update_post(d["post_id"], status="rejected", reason="отклонил Некит")
        drop_keep(self.s, d["post_id"])
        return f"{text}\n\n❌ Отклонено", None

    def on_alt(self, did: int):
        d = self.db.get_draft(did)
        return (None, alts_kb(did, json.loads(d["alts"]))) if d else ("Черновик не найден", None)

    def on_back(self, did: int):
        d = self.db.get_draft(did)
        return (None, self._card(d)[1]) if d else ("Черновик не найден", None)

    def on_to(self, did: int, i: int):
        d = self.db.get_draft(did)
        alts = json.loads(d["alts"]) if d else []
        if not 0 <= i < len(alts):
            return "Вариант устарел", None
        mode, target = alts[i]
        fields = {"mode": mode, "target": target}
        if mode == "new" and not d["title"]:
            fields["title"] = d["content"].strip().splitlines()[0][:80]
        self.db.update_draft(did, **fields)
        return self._card(self.db.get_draft(did))

    def on_rb(self, bid: int):
        return None, confirm_kb(bid)

    def on_rbn(self, bid: int):
        return None, written_kb(bid)

    def _rollback_text(self, res: str) -> str:
        return {"ok": "↩️ Откатил", "already": "Уже откачено", "gone": "⚠️ Файла уже нет",
                "changed": f"⚠️ Блок правили руками, откати вручную. Снапшоты: {self.s.snapshots_dir}"}[res]

    def on_rby(self, bid: int):
        return self._rollback_text(self.writer.rollback(bid)), None

    def on_vy(self, pid: int):
        self.db.update_post(pid, video_state="yes")
        return "🎬 Разберу видео, карточка придёт после ночного разбора", None

    def on_vn(self, pid: int):
        self.db.update_post(pid, status="rejected", reason="видео: не надо", video_state="no")
        drop_keep(self.s, pid)
        return "❌ Пропускаю", None

    def on_pl(self, pid: int, useful: int):
        self.db.add_label(pid, bool(useful))
        return ("👍" if useful else "👎") + " учтено", None

    def on_ab(self, aid: int):
        a = self.db.get_audit(aid)
        if a is None:
            return "Не найдено", None
        res = self.writer.rollback(a["block_id"])
        if res in ("ok", "already"):
            self.db.update_audit(aid, status="rolled_back")
        return self._rollback_text(res), None

    def on_ak(self, aid: int):
        self.db.update_audit(aid, status="kept")
        return "Оставил", None


def build_dispatcher(app: App) -> Dispatcher:
    r = Router()
    owner = F.from_user.id == app.s.owner_id

    @r.callback_query(owner)
    async def _cb(cq: CallbackQuery):
        text, kb = app.handle_callback(cq.data or "")
        if text is not None:
            await cq.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        elif kb is not None:
            await cq.message.edit_reply_markup(reply_markup=kb)
        await cq.answer()

    @r.message(owner, Command("status"))
    async def _status(m: Message):
        await m.answer(app.status())

    @r.message(owner, Command("pilot"))
    async def _pilot(m: Message):
        await m.answer("🧪 Шлю выборку постов, отметь каждый: полезно или мусор")
        await app.pilot(m.bot)

    @r.message(owner, Command("calibrate"))
    async def _calibrate(m: Message):
        res = calibrate(app.s, app.db)
        await m.answer(f"Новые пороги: {res}" if res else "Мало разметки: нужен хотя бы один 👍 и один 👎")

    dp = Dispatcher()
    dp.include_router(r)
    return dp
```

- [ ] **Step 5: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(harvester): бот ревью с откатом, пилотом и отчётами

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Аудитор (воскресная проверка записанного)

**Files:**
- Create: `harvester/auditor.py`
- Test: `tests/test_auditor.py`

**Interfaces:**
- Consumes: `brain.run_claude/BrainError/chunks`, `DB.blocks_since/add_audit/kv_set`
- Produces: `block_view(s, block) -> str | None`, `run_audit(s, db, claude=run_claude, days=7, now=datetime.now) -> dict` (отчёт также в `kv["audit_report"]`; сомнительные блоки в таблице `audits`, их показывает бот из задачи 8). Лимит вызовов: `max(1, max_calls_per_night // 2)`.

- [ ] **Step 1: Падающие тесты**

`tests/test_auditor.py`:

```python
import asyncio
import json

from harvester.auditor import run_audit
from harvester.brain import BrainError
from tests.helpers import FakeClaude


def test_audit_records_doubts_for_active_blocks_only(s, db):
    (s.vault / "n.md").write_text("# N\n\nтекст\n\n## Из Telegram\n\n- блок\n", "utf-8")
    b1 = db.add_block(1, "n.md", "append", 15, 40, "x", {"text": "\n## Из Telegram\n\n- блок\n", "line": "- блок\n"})
    b2 = db.add_block(2, "n.md", "append", 15, 40, "x", {"text": "z", "line": "z"})
    db.mark_rolled_back(b2)
    b3 = db.add_block(3, "gone.md", "append", 0, 1, "x", {"text": "q", "line": "q"})
    fake = FakeClaude([lambda p: {"items": [{"block_id": b1, "verdict": "doubt", "reason": "не по теме"},
                                            {"block_id": 999, "verdict": "doubt", "reason": "чужой"}]}])
    rep = asyncio.run(run_audit(s, db, claude=fake))
    prompt = fake.calls[0][0]
    assert f"block_id {b1} " in prompt and f"block_id {b2} " not in prompt and f"block_id {b3} " not in prompt
    assert "- блок" in prompt and "текст" in prompt
    audits = db.audits("new")
    assert [(a["block_id"], a["reason"]) for a in audits] == [(b1, "не по теме")]
    assert (rep["checked"], rep["doubt"], rep["calls"]) == (1, 1, 1)
    assert json.loads(db.kv_get("audit_report"))["doubt"] == 1


def test_audit_stops_on_error(s, db):
    (s.vault / "n.md").write_text("x", "utf-8")
    db.add_block(1, "n.md", "new", 0, 1, "x", {"item": "[[n]]"})
    rep = asyncio.run(run_audit(s, db, claude=FakeClaude([BrainError("usage limit")])))
    assert rep["aborted"] and db.audits("new") == []
```

- [ ] **Step 2: Убедиться, что падают**

Run: `uv run pytest tests/test_auditor.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.auditor'`

- [ ] **Step 3: `harvester/auditor.py`**

```python
"""Воскресный аудит: Claude перечитывает блоки за неделю и отмечает сомнительные."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .brain import BrainError, chunks, run_claude
from .config import Settings
from .db import DB

AUDIT_SYSTEM = """Ты проверяешь, что бот дописал в заметки Obsidian за неделю. Для каждого блока verdict ok, если блок по теме заметки, не повторяет то, что в заметке уже есть, и не обрывается на полуслове. Иначе verdict doubt и короткий reason по-русски. Не используй длинное тире. Строго по JSON-схеме."""

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"block_id": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["ok", "doubt"]},
                       "reason": {"type": "string"}},
        "required": ["block_id", "verdict", "reason"],
    }}},
    "required": ["items"],
}


def block_view(s: Settings, b) -> str | None:
    path = s.vault / b["path"]
    if not path.exists():
        return None
    note = path.read_text("utf-8", errors="ignore")
    extra = json.loads(b["extra"])
    if b["kind"] == "append":
        before = note.replace(extra.get("text", ""), "", 1)
        return (f"### block_id {b['id']} · дописано в «{b['path']}»\nЗаметка без блока:\n{before[:3000]}\n\n"
                f"Дописанный блок:\n{extra.get('line', '')}")
    return f"### block_id {b['id']} · новая заметка «{b['path']}»\n{note[:3000]}"


async def run_audit(s: Settings, db: DB, claude=run_claude, days: int = 7, now=datetime.now) -> dict:
    since = (now() - timedelta(days=days)).isoformat(timespec="seconds")
    limit = max(1, s.max_calls_per_night // 2)
    rep = {"date": now().date().isoformat(), "checked": 0, "doubt": 0, "calls": 0, "limit": limit, "aborted": None}
    for chunk in chunks(db.blocks_since(since), 10):
        if rep["calls"] >= limit:
            break
        views = [(b, v) for b in chunk if (v := block_view(s, b))]
        if not views:
            continue
        rep["calls"] += 1
        try:
            raw = await claude(s, "\n\n".join(v for _, v in views), system=AUDIT_SYSTEM, schema=AUDIT_SCHEMA,
                               timeout=s.claude_timeout)
        except BrainError as e:
            rep["aborted"] = str(e)[:200]
            break
        ids = {b["id"] for b, _ in views}
        for it in (raw.get("items") if isinstance(raw, dict) else None) or []:
            if it.get("block_id") not in ids:
                continue
            rep["checked"] += 1
            if it.get("verdict") == "doubt":
                db.add_audit(it["block_id"], "doubt", str(it.get("reason") or "")[:300])
                rep["doubt"] += 1
    db.kv_set("audit_report", json.dumps(rep, ensure_ascii=False))
    return rep
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(harvester): еженедельный аудит записанного

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: CLI, live-процесс, launchd

**Files:**
- Create: `harvester/live.py`, `harvester/cli.py`, `harvester/__main__.py`, `scripts/setup.sh`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: всё из задач 1-9
- Produces: `cli.main(argv: list[str] | None = None, root: Path | None = None) -> int`; команды `login | live | night | audit | calibrate | section <имя>`; `live.run_live(s)`; `scripts/setup.sh install|restart|stop|status|logs|night-now|uninstall`

- [ ] **Step 1: Падающий тест CLI**

`tests/test_cli.py`:

```python
import shutil
from pathlib import Path

from harvester.cli import main

REPO = Path(__file__).resolve().parent.parent


def test_section_command_creates_music(tmp_path, monkeypatch, capsys):
    shutil.copytree(REPO / "config", tmp_path / "config")
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_DIR", str(vault))
    assert main(["section", "Музыка"], root=tmp_path) == 0
    assert (vault / "Музыка.canvas").exists()
    assert "Музыка.canvas" in capsys.readouterr().out
    assert main(["section", "Музыка"], root=tmp_path) == 0
    assert "уже есть" in capsys.readouterr().out


def test_calibrate_command_without_labels(tmp_path, monkeypatch, capsys):
    shutil.copytree(REPO / "config", tmp_path / "config")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    assert main(["calibrate"], root=tmp_path) == 0
    assert "Мало разметки" in capsys.readouterr().out
```

- [ ] **Step 2: Убедиться, что падает**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'harvester.cli'`

- [ ] **Step 3: `harvester/live.py`**

```python
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
```

- [ ] **Step 4: `harvester/cli.py` и `harvester/__main__.py`**

```python
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
    await client.start()  # спросит телефон, код из Telegram и пароль 2FA прямо в терминале
    me = await client.get_me()
    print(f"✅ Вошёл как {me.first_name}. Сессия: {s.session_path}.session")
    await client.disconnect()


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    p = argparse.ArgumentParser(prog="harvester")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("login", "live", "night", "audit", "calibrate"):
        sub.add_parser(name)
    sec = sub.add_parser("section")
    sec.add_argument("name")
    a = p.parse_args(argv)
    s = load_settings(root)
    _logging(s, a.cmd)

    if a.cmd == "section":
        from .vault_setup import ensure_section
        created = ensure_section(s.vault, a.name)
        print("Создано: " + ", ".join(created) if created else f"Раздел «{a.name}» уже есть")
    elif a.cmd == "login":
        asyncio.run(_login(s))
    elif a.cmd == "live":
        from .live import run_live
        asyncio.run(run_live(s))
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
```

`harvester/__main__.py`:

```python
import sys

from .cli import main

sys.exit(main())
```

- [ ] **Step 5: Тесты зелёные**

Run: `uv run pytest -q`
Expected: PASS (все тесты проекта)

- [ ] **Step 6: `scripts/setup.sh`**

```zsh
#!/bin/zsh
# TG Harvester: автозапуск трёх процессов.
#   scripts/setup.sh install | restart | stop | status | logs | night-now | uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
DOMAIN="gui/$(id -u)"
BASE="com.cucaramba.tg-harvester"
LABELS=("$BASE" "$BASE.night" "$BASE.audit")

plist_path() { echo "$HOME/Library/LaunchAgents/$1.plist"; }

write_plist() {  # $1 label, $2 команда, $3 xml расписания
  mkdir -p "$ROOT/logs"
  cat > "$(plist_path "$1")" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-m</string><string>harvester</string><string>$2</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  $3
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd-$2.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd-$2.err.log</string>
</dict>
</plist>
EOF
}

case "${1:-install}" in
  install)
    cd "$ROOT"
    [[ -f .env ]] || { cp .env.example .env; echo "Заполни BOT_TOKEN, TG_API_ID, TG_API_HASH в $ROOT/.env и запусти install ещё раз"; exit 1; }
    [[ -f data/harvester.session ]] || { echo "Сначала: uv run python -m harvester login"; exit 1; }
    [[ -x "$PY" ]] || uv sync
    write_plist "$BASE" live "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>"
    write_plist "$BASE.night" night "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>"
    write_plist "$BASE.audit" audit "<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>"
    for l in $LABELS; do
      launchctl bootout "$DOMAIN/$l" 2>/dev/null || true
      launchctl bootstrap "$DOMAIN" "$(plist_path "$l")"
    done
    echo "✅ TG Harvester установлен: live работает, ночь в 03:00, аудит по воскресеньям в 04:00"
    ;;
  restart) launchctl kickstart -k "$DOMAIN/$BASE" && echo "🔄 live перезапущен" ;;
  stop) for l in $LABELS; do launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; done; echo "⏹ остановлен" ;;
  status) for l in $LABELS; do echo "== $l"; launchctl print "$DOMAIN/$l" 2>/dev/null | grep -E "state|pid|last exit" || echo "не загружен"; done ;;
  logs) tail -n 50 -f "$ROOT/logs/harvester-live.log" ;;
  night-now) launchctl kickstart "$DOMAIN/$BASE.night" && echo "🌙 ночной разбор запущен, лог: logs/harvester-night.log" ;;
  uninstall)
    for l in $LABELS; do launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; rm -f "$(plist_path "$l")"; done
    echo "🗑 автозапуск удалён, data/ не тронута"
    ;;
  *) echo "usage: $0 install|restart|stop|status|logs|night-now|uninstall"; exit 1 ;;
esac
```

```bash
chmod +x scripts/setup.sh
zsh -n scripts/setup.sh && echo syntax ok
```

Expected: `syntax ok`

- [ ] **Step 7: `README.md`**

```markdown
# TG Harvester

Собирает посты из Telegram-каналов (`config/channels.yaml`), бесплатно фильтрует их на маке, ночью разбирает лучшие через `claude -p` и присылает черновики в бота. После ✅ пишет в Obsidian.

Спека: `docs/specs/2026-10-10-tg-harvester-design.md`. План: `docs/plans/2026-10-10-tg-harvester.md`.

## Запуск с нуля

1. `cp .env.example .env`, заполнить `BOT_TOKEN` (новый бот у @BotFather), `TG_API_ID` и `TG_API_HASH` (те же, что у telegram-mcp).
2. `uv sync && uv run python -m harvester login`: телефон, код из Telegram, пароль 2FA.
3. `uv run python -m harvester section Музыка`: раздел L2 «Музыка».
4. `scripts/setup.sh install`.

## Команды бота

- `/status`: сколько постов в каком статусе, фазы каналов.
- `/pilot`: 50 случайных постов на разметку 👍/👎.
- `/calibrate`: подобрать пороги фильтра по разметке.

## Обслуживание

- `scripts/setup.sh status | logs | restart | night-now | stop`.
- Лимит Claude за ночь: `max_calls_per_night` в `config/settings.yaml`.
- Добавить канал: строка в `config/channels.yaml`, потом `scripts/setup.sh restart`.
- Снапшоты заметок до правок: `data/snapshots/<дата>/`, хранятся 30 дней.
```

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(harvester): CLI, live-процесс и launchd

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Живой прогон с Некитом (пилот и калибровка)

Делается только вместе с Некитом. Живой vault впервые трогается здесь.

**Files:**
- Modify: `.env` (Некит), `config/settings.yaml` (`max_calls_per_night` по итогам пилота), `HANDOFF.md`

- [ ] **Step 1: Проверки перед стартом**

```bash
df -h ~ | tail -1               # свободно должно быть больше 10 GB
cd "/Users/cucaramba/Claude Code/tg-harvester" && uv run pytest -q
```

Expected: свободно больше 10 GB, все тесты зелёные. Если места меньше, остановиться и сказать Некиту.

- [ ] **Step 2: Бот и ключи (делает Claude через telegram-mcp, Некит разрешил)**

1. Проверить, что в списке тулов есть `mcp__telegram-mcp__send_message`. Написать @BotFather `/newbot`, имя `TG Harvester`, username вида `cucaramba_harvester_bot` (если занят, добавить цифры). Токен из ответа BotFather записать в `.env` как `BOT_TOKEN`. Так же создавали `@cucaramba_idea_bot`.
2. `TG_API_ID` и `TG_API_HASH`: взять значения `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` из `~/mcp-servers/telegram-mcp/.env` и записать в `.env` проекта. Значения в чат не выводить. Если auto mode заблокирует чтение или копирование секретов, дать Некиту готовую команду, он выполнит сам.
3. Написать новому боту `/start` с аккаунта Некита (через `send_message`), чтобы бот мог ему писать.

- [ ] **Step 3: Логин Telethon (Claude, код берёт из Telegram сам)**

Код входа приходит в чат «Telegram» (id 777000). Скрипт в scratchpad, не в проекте:

```python
# login_step.py: шаг 1 запрашивает код, шаг 2 входит с кодом
import asyncio, sys
from telethon import TelegramClient
from harvester.config import load_settings

async def main():
    s = load_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    c = TelegramClient(str(s.session_path), s.api_id, s.api_hash)
    await c.connect()
    if sys.argv[1] == "send":
        r = await c.send_code_request(sys.argv[2])
        (s.data_dir / "phone_code_hash").write_text(r.phone_code_hash)
    else:
        h = (s.data_dir / "phone_code_hash").read_text()
        await c.sign_in(phone=sys.argv[2], code=sys.argv[3], phone_code_hash=h)
        (s.data_dir / "phone_code_hash").unlink()
        print("ok", (await c.get_me()).first_name)
    await c.disconnect()

asyncio.run(main())
```

1. Номер телефона: из `mcp__telegram-mcp__get_me` (поле phone), если его там нет, спросить Некита.
2. `cd ~/Claude\ Code/tg-harvester && uv run python <scratchpad>/login_step.py send <phone>`.
3. Прочитать последнее сообщение в чате 777000 (`get_history`, limit 3), взять 5-значный код.
4. `uv run python <scratchpad>/login_step.py sign <phone> <код>`. Expected: `ok <имя>`.
5. Если Telethon ответил `SessionPasswordNeededError` (включён 2FA): спросить пароль у Некита, а лучше попросить его самого выполнить `uv run python -m harvester login` в терминале. Пароль в чат не просить без крайней нужды.
6. В Telegram придёт уведомление о новом входе, это наша сессия. Сказать Некиту.

- [ ] **Step 4: Раздел «Музыка» в живом vault**

```bash
uv run python -m harvester section Музыка
```

Expected: `Создано: Музыка/Музыка — карта заметок.md, Музыка.canvas, Обо мне.canvas`. Открыть «Обо мне» в Obsidian и убедиться, что карточка «Музыка» встала в группу «Сферы жизни» без наложений.

- [ ] **Step 5: Установка и первые часы**

```bash
scripts/setup.sh install
scripts/setup.sh status
tail -n 30 logs/harvester-live.log
```

Expected: все три label загружены, live в `state = running`, в логе «TG Harvester запущен» и строки «канал ...: старт с id ...». Через час `/status` в боте показывает растущие `filtered`, `explore`, `candidate`.

- [ ] **Step 6: Пилот разметки**

Когда в `/status` больше 300 постов с совпадением: Некит шлёт `/pilot`, размечает 50 карточек, затем `/calibrate`. Записать новые пороги в HANDOFF.

- [ ] **Step 7: Пробная ночь с лимитом 5**

`max_calls_per_night: 5` уже стоит. Перед ночью Некит записывает текущий % недельного лимита в claude.ai (Settings → Usage). Утром: отчёт «🌙 Ночь» в боте и новый % лимита. Посчитать: `% за один вызов = (утро - вечер) / calls`. База `max_calls_per_night = floor(10% недели / 7 ночей / % за вызов)`. Ретро-множитель 2.5 даёт примерно 25%. Вписать в `config/settings.yaml`, `scripts/setup.sh restart`.

- [ ] **Step 8: Ревью первых карточек вместе**

Некит жмёт ✅/❌/✏️ на утренних карточках. Проверить в Obsidian: дописанная заметка не изменилась выше `## Из Telegram`, новая заметка есть в MOC и на холсте раздела, ↩️ Откатить возвращает файл как был.

- [ ] **Step 9: Финал**

- Зафиксировать стек в холсте «Скиллы на машине» не нужно: это не скилл, а проект.
- Обновить память (`dungeon_tasks`-стиль): файл `tg_harvester_project.md` с путём проекта и точкой входа `HANDOFF.md`, строка в `MEMORY.md`.
- HANDOFF: статус «работает», откалиброванные пороги и лимит, открытые вопросы.

```bash
git add -A
git commit -m "chore(harvester): живой запуск, пороги и лимит по пилоту

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
