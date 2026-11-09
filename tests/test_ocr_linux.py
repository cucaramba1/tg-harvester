import subprocess

import pytest
from PIL import Image

from harvester import ocr_linux


def img(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGB", (40, 20), "white").save(p)
    return p


def test_command_and_cleanup(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        with Image.open(cmd[1]) as im:
            seen["size"], seen["mode"] = im.size, im.mode  # увеличено вдвое и серое
        return subprocess.CompletedProcess(cmd, 0, stdout="  привет \n\n мир\n", stderr="")

    monkeypatch.setattr(ocr_linux.subprocess, "run", fake_run)
    assert ocr_linux.ocr_image(img(tmp_path)) == "привет\nмир"
    assert seen["cmd"][2:] == ["-", "-l", "rus+eng", "--psm", "11"]
    assert seen["size"] == (80, 40) and seen["mode"] == "L"


def test_missing_binary_is_clear_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ocr_linux.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="tesseract не установлен"):
        ocr_linux.ocr_image(img(tmp_path))


def test_nonzero_exit_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_linux.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 1, "", "bad"))
    with pytest.raises(RuntimeError, match="tesseract: bad"):
        ocr_linux.ocr_image(img(tmp_path))
