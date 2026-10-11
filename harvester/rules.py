"""Дешёвые правила до любой обработки: реклама и розыгрыши."""
from __future__ import annotations


def ad_reason(text: str, markers: list[str]) -> str | None:
    low = (text or "").lower()
    return next((f"реклама: {m.lower()}" for m in markers if m.lower() in low), None)
