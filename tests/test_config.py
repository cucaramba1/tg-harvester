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
