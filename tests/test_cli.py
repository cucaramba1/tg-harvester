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


def test_bot_command_runs_bot_only(tmp_path, monkeypatch):
    shutil.copytree(REPO / "config", tmp_path / "config")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    called = []

    async def fake_run_bot(s):
        called.append(s)

    monkeypatch.setattr("harvester.live.run_bot", fake_run_bot)
    assert main(["bot"], root=tmp_path) == 0
    assert len(called) == 1
