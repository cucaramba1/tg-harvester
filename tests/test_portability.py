"""Кроссплатформенные места: проверяем логику выбора ветки, сами Windows-бэкенды проверяются на Windows."""
import sys
from types import SimpleNamespace

import pytest

from harvester import enricher, topic_check
from harvester.config import tool_path


def test_tool_path_posix_adds_system_dirs(monkeypatch):
    monkeypatch.setattr("harvester.config.os.name", "posix")
    p = tool_path("/x/bin")
    assert p.split(":")[0] == "/x/bin" and "/usr/bin" in p


def test_tool_path_windows_keeps_env_path(monkeypatch):
    monkeypatch.setattr("harvester.config.os.name", "nt")
    monkeypatch.setattr("harvester.config.os.pathsep", ";")
    monkeypatch.setenv("PATH", r"C:\Windows")
    assert tool_path(r"C:\npm") == r"C:\npm;C:\Windows"


def test_ocr_on_other_platform_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(enricher.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "harvester.ocr_windows", None)  # модуля ещё нет: ImportError
    with pytest.raises(RuntimeError, match="OCR"):
        enricher.ocr_image(tmp_path / "a.png")


def test_foreign_whisper_sees_both_variants(monkeypatch):
    def procs(cmds):
        return lambda attrs: [SimpleNamespace(info={"cmdline": c}) for c in cmds]
    monkeypatch.setattr(topic_check.psutil, "process_iter", procs([["python", "-m", "harvester"], None]))
    assert topic_check.foreign_whisper() is False
    monkeypatch.setattr(topic_check.psutil, "process_iter", procs([["python", "whisper_cli.py", "a.mp3"]]))
    assert topic_check.foreign_whisper() is True
    monkeypatch.setattr(topic_check.psutil, "process_iter", procs([["/x/mlx_whisper", "a.mp3"]]))
    assert topic_check.foreign_whisper() is True


def test_mem_free_pct_uses_psutil_off_mac(monkeypatch):
    monkeypatch.setattr(topic_check.sys, "platform", "win32")
    monkeypatch.setattr(topic_check.psutil, "virtual_memory", lambda: SimpleNamespace(available=25, total=100))
    assert topic_check.mem_free_pct() == 25.0


def test_extra_vaults_split_by_pathsep(monkeypatch, tmp_path):
    import inspect
    from harvester import config
    assert "split(os.pathsep)" in inspect.getsource(config) and 'split(":")' not in inspect.getsource(config)
