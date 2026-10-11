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
