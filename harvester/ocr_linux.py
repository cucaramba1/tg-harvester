"""OCR на Linux (сервер): Tesseract rus+eng. Серый + увеличение x2 + psm 11 (разреженный текст).
Замер 07.11.2026 на 30 картинках против ocrmac: доля найденных слов 0.67 (базово) -> 0.75; скрипт tools/server-trial/bench/ocr_compare.py."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image


def ocr_image(path: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        big = Path(tmp) / "o.png"
        with Image.open(path) as im:
            g = im.convert("L")
            g.resize((g.width * 2, g.height * 2), Image.LANCZOS).save(big)
        try:
            r = subprocess.run(["tesseract", str(big), "-", "-l", "rus+eng", "--psm", "11"],
                               capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            raise RuntimeError("tesseract не установлен (apt install tesseract-ocr tesseract-ocr-rus)")
        except subprocess.TimeoutExpired:
            raise RuntimeError("tesseract не успел за 2 минуты")
    if r.returncode != 0:
        raise RuntimeError(f"tesseract: {r.stderr[-300:]}")
    return "\n".join(line.strip() for line in r.stdout.splitlines() if line.strip())
