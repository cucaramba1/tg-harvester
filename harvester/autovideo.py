"""Видео-вопросы: калибровочная выдача (набираем ответы по каналам) и ячейки, по которым считается правило."""
from __future__ import annotations

import random
from collections import defaultdict
from itertools import zip_longest

from .config import Settings
from .db import DB

SIM_EDGES = (0.55, 0.65, 0.75)  # границы корзин похожести: <0.55, 0.55-0.65, 0.65-0.75, >=0.75
LONG_CAPTION = 200


def cell_of(score: float | None, text: str) -> tuple[int, int]:
    """Ячейка поста: (корзина похожести, длинная ли подпись)."""
    sim = sum((score or 0.0) >= e for e in SIM_EDGES)
    return sim, int(len((text or "").strip()) >= LONG_CAPTION)


def _calib_channels(s: Settings, db: DB) -> list[int]:
    if s.video_calib_channels:
        return list(s.video_calib_channels)
    rows = db.c.execute("SELECT chat_id FROM posts WHERE status='video_ask' AND video_state='ask' "
                        "GROUP BY chat_id ORDER BY COUNT(*) DESC LIMIT ?", (s.video_calib_top,)).fetchall()
    return [r[0] for r in rows]


def _need(s: Settings, db: DB, chat_id: int) -> int:
    answered = db.c.execute("SELECT COUNT(*) FROM labels l JOIN posts p ON p.id=l.post_id WHERE p.chat_id=?",
                            (chat_id,)).fetchone()[0]
    in_flight = db.c.execute("SELECT COUNT(*) FROM posts WHERE chat_id=? AND status='video_ask' "
                             "AND video_state='asked'", (chat_id,)).fetchone()[0]
    return s.video_calib_n - answered - in_flight


def _spread(rows: list) -> list:
    """Лучшие по похожести в каждой ячейке, ячейки по кругу: вкус проверяем и на скучных постах тоже."""
    cells: dict[tuple[int, int], list] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (-(r["score"] or 0.0), r["id"])):
        cells[cell_of(r["score"], r["text"])].append(r)
    return [r for round_ in zip_longest(*(cells[k] for k in sorted(cells))) for r in round_ if r is not None]


def calibration_picks(s: Settings, db: DB, limit: int) -> list:
    """Каналы, где ответов меньше video_calib_n, получают вопросы вперёд очереди, вразброс по ячейкам."""
    per_channel = []
    for chat_id in _calib_channels(s, db):
        need = _need(s, db, chat_id)
        if need <= 0:
            continue
        rows = db.c.execute("SELECT * FROM posts WHERE chat_id=? AND status='video_ask' AND video_state='ask'",
                            (chat_id,)).fetchall()
        per_channel.append(_spread(rows)[:need])
    mixed = [r for round_ in zip_longest(*per_channel) for r in round_ if r is not None]
    return mixed[:limit]


def smoothed(yes: int, n: int) -> float:
    """Доля полезных со сглаживанием: пустая ячейка 0.5, 8 из 8 даёт 0.9, 0 из 8 даёт 0.1."""
    return (yes + 1) / (n + 2)


def _rows(db: DB) -> list[tuple]:
    """Ответы Некита для обучения: (chat_id, score, text, useful). Авто-решения бота в обучение не идут."""
    return [(r["chat_id"], r["score"], r["text"], bool(r["useful"])) for r in db.c.execute(
        "SELECT p.chat_id, p.score, p.text, l.useful FROM labels l JOIN posts p ON p.id=l.post_id "
        "WHERE l.source != 'auto' ORDER BY l.post_id")]


class Rule:
    """Решает по каналу и ячейке: доля полезных по ответам Некита. Мало ответов в ячейке значит корзина похожести канала,
    и там мало значит спросить."""

    def __init__(self, s: Settings, rows: list[tuple]):
        self.conf, self.min_n = s.video_auto_conf, s.video_auto_min_n
        self.cell: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        self.bucket: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        for chat, score, text, useful in rows:
            sim, long_ = cell_of(score, text)
            for d, key in ((self.cell, (chat, sim, long_)), (self.bucket, (chat, sim))):
                d[key][0] += int(useful)
                d[key][1] += 1

    @classmethod
    def build(cls, s: Settings, db: DB) -> "Rule":
        return cls(s, _rows(db))

    def _verdict(self, yes: int, n: int) -> str:
        if n < self.min_n:
            return "ask"
        p = smoothed(yes, n)
        if p >= self.conf - 1e-9:
            return "yes"
        if p <= 1 - self.conf + 1e-9:
            return "no"
        return "ask"

    def decide(self, post) -> tuple[str, str]:
        sim, long_ = cell_of(post["score"], post["text"])
        chat = post["chat_id"]
        for label, (yes, n) in (("ячейка", self.cell.get((chat, sim, long_), (0, 0))),
                                ("корзина похожести", self.bucket.get((chat, sim), (0, 0)))):
            v = self._verdict(yes, n)
            if n >= self.min_n:
                return v, f"{label} ({sim}, {'длинная' if long_ else 'короткая'} подпись): {yes} полезных из {n}"
        return "ask", "мало ответов по каналу"


def holdout(s: Settings, db: DB, seed: int = 0, share: float = 0.2) -> dict[int, dict]:
    """Проверка честности: учимся на 80% ответов канала, решаем за остальные 20% и считаем ошибки."""
    by_chat: dict[int, list[tuple]] = defaultdict(list)
    for r in _rows(db):
        by_chat[r[0]].append(r)
    rng = random.Random(seed)
    train, test = [], []
    for chat in sorted(by_chat):
        rows = by_chat[chat][:]
        rng.shuffle(rows)
        k = round(len(rows) * share)
        test += rows[:k]
        train += rows[k:]
    rule = Rule(s, train)
    out: dict[int, dict] = defaultdict(lambda: {"test": 0, "yes": 0, "no": 0, "ask": 0, "yes_wrong": 0, "no_wrong": 0})
    for chat, score, text, useful in test:
        v, _ = rule.decide({"chat_id": chat, "score": score, "text": text})
        o = out[chat]
        o["test"] += 1
        o[v] += 1
        o["yes_wrong"] += int(v == "yes" and not useful)
        o["no_wrong"] += int(v == "no" and useful)
    return dict(out)


def report_text(s: Settings, db: DB) -> str:
    """Что правило знает по каждому каналу, насколько ему можно верить и сколько вопросов уйдёт из текущей очереди."""
    rows = _rows(db)
    rule = Rule(s, rows)
    chats = sorted({r[0] for r in rows} | {r["chat_id"] for r in db.c.execute(
        "SELECT DISTINCT chat_id FROM posts WHERE status='video_ask' AND video_state='ask'")})
    hold = holdout(s, db)
    lines = [f"Порог {s.video_auto_conf:.0%}, минимум ответов в ячейке {s.video_auto_min_n}, режим: {s.video_auto}", ""]
    tot = {"yes": 0, "no": 0, "ask": 0}
    for chat in chats:
        answered = sum(1 for r in rows if r[0] == chat)
        queue = db.c.execute("SELECT * FROM posts WHERE chat_id=? AND status='video_ask' AND video_state='ask'",
                             (chat,)).fetchall()
        q = {"yes": 0, "no": 0, "ask": 0}
        for p in queue:
            q[rule.decide(p)[0]] += 1
        for k in q:
            tot[k] += q[k]
        lines.append(f"{s.channel_name(chat)}: ответов {answered}, в очереди {len(queue)} "
                     f"(сам взял {q['yes']}, сам отклонил {q['no']}, спросит {q['ask']})")
        h = hold.get(chat)
        if h and h["test"]:
            lines.append(f"  проверка на отложенных {h['test']}: авто-✅ {h['yes']} (ошибок {h['yes_wrong']}), "
                         f"авто-❌ {h['no']} (ошибок {h['no_wrong']}), спросил бы {h['ask']}")
    lines += ["", f"Очередь в целом: сам взял {tot['yes']}, сам отклонил {tot['no']}, спросит {tot['ask']}"]
    return "\n".join(lines)
