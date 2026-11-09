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
        vs = np.array(list(self.m.embed(texts, batch_size=16)), dtype=np.float32)  # дефолт 256 даёт пик ~+600 МБ
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
        for p, rel in ((p, p.relative_to(r)) for r in self.s.vaults for p in r.rglob("*.md")):
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
            vecs = data["vecs"]  # один раз: у npz каждое data["vecs"] заново читает весь массив, в цикле это +4 МБ на каждую заметку (2600 заметок = 10 ГБ)
            return {r: (mt, vecs[i]) for i, (r, mt) in enumerate(meta)}
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
            vecs = self.embedder.embed([note_text(self.s.path(r)) for r in todo])
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
        self.sections = list_sections(self.s.vaults)
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


DUP_MIN_CHARS = 80  # короткие подписи («🔥», «смотрите») похожи на всё подряд, их не склеиваем


class DupIndex:
    """Дубли кандидатов (репост, одна новость из разных каналов, повтор рекламы) до ночного claude -p.
    Идея из NyanNyanovich/nyan (clusterer.py), без кластеризации: первый в очереди остаётся, похожие уходят в duplicate."""

    def __init__(self, threshold: float, rows=()):
        self.t = threshold
        self.ids: list[int] = []
        self.m: np.ndarray | None = None  # буфер с запасом: vstack на каждый пост при 10k очереди копировал бы гигабайты
        for r in rows:
            self.add(r["id"], np.frombuffer(r["emb"], dtype=np.float32))

    def find(self, vec: np.ndarray) -> int | None:
        if not self.ids:
            return None
        sims = self.m[: len(self.ids)] @ vec
        i = int(np.argmax(sims))
        return self.ids[i] if sims[i] >= self.t else None

    def add(self, pid: int, vec: np.ndarray) -> None:
        n = len(self.ids)
        if self.m is None:
            self.m = np.empty((256, vec.shape[0]), dtype=np.float32)
        elif n == len(self.m):
            self.m = np.concatenate([self.m, np.empty_like(self.m)])
        self.m[n] = vec
        self.ids.append(pid)


def mark_duplicate(s: Settings, db: DB, pid: int, of: int) -> None:
    db.update_post(pid, status="duplicate", reason=f"дубль #{of}")
    drop_keep(s, pid)


class Matcher:
    def __init__(self, s: Settings, db: DB, vectors: VaultVectors, dups: DupIndex | None = None):
        self.s, self.db, self.v = s, db, vectors
        self.dups = dups if dups is not None else DupIndex(s.dup_threshold, db.dup_pool())

    def process(self, post) -> str:
        text = "\n".join(x for x in (post["text"], post["extra_text"]) if x).strip()
        vec = _norm(self.v.embedder.embed([text[:2000]])[0])
        m = self.v.match(vec)
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
        if status == "candidate" and len(text) >= DUP_MIN_CHARS:
            of = self.dups.find(vec)
            if of is not None:
                self.db.update_post(post["id"], **fields)
                mark_duplicate(self.s, self.db, post["id"], of)
                return "duplicate"
            fields["emb"] = vec.astype(np.float32).tobytes()
            self.dups.add(post["id"], vec)
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
