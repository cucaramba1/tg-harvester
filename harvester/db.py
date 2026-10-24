"""SQLite: посты, каналы, черновики, блоки, разметка, аудит, алерты, kv.

WAL и busy_timeout: live-процесс и ночной процесс пишут в одну базу."""
from __future__ import annotations

import json
import re
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
CREATE TABLE IF NOT EXISTS labels(
  post_id INTEGER PRIMARY KEY,
  useful INTEGER NOT NULL,
  reason TEXT,
  source TEXT NOT NULL DEFAULT 'import',
  ts TEXT
);
CREATE TABLE IF NOT EXISTS post_meta(post_id INTEGER PRIMARY KEY, doc_id INTEGER, size INTEGER, dur REAL);
CREATE TABLE IF NOT EXISTS topics(post_id INTEGER PRIMARY KEY, topic TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS topics_topic ON topics(topic);
CREATE TABLE IF NOT EXISTS topic_state(
  id INTEGER PRIMARY KEY,
  topic TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL DEFAULT 'new',
  msg_id INTEGER,
  report TEXT
);
CREATE TABLE IF NOT EXISTS topic_checks(
  post_id INTEGER PRIMARY KEY,
  silent INTEGER NOT NULL DEFAULT 0,
  speech TEXT NOT NULL DEFAULT '',
  ocr TEXT NOT NULL DEFAULT ''
);
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
        self._migrate()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.c.execute("PRAGMA table_info(labels)")}
        for col, ddl in (("reason", "TEXT"), ("source", "TEXT NOT NULL DEFAULT 'import'"), ("ts", "TEXT")):
            if col not in have:
                self.c.execute(f"ALTER TABLE labels ADD COLUMN {col} {ddl}")
        if "emb" not in {r["name"] for r in self.c.execute("PRAGMA table_info(posts)")}:
            self.c.execute("ALTER TABLE posts ADD COLUMN emb BLOB")  # вектор кандидата для поиска дублей

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
        return self.c.execute("SELECT * FROM posts WHERE status='video_ask' AND video_state='ask' ORDER BY score DESC, id LIMIT ?",
                              (limit,)).fetchall()

    def video_ask_count(self) -> int:
        return self.c.execute("SELECT COUNT(*) FROM posts WHERE status='video_ask' AND video_state='ask'").fetchone()[0]

    def video_asked_count(self) -> int:
        """Карточки «разобрать видео?», на которые ещё не нажали ни «Разобрать», ни «Не надо»."""
        return self.c.execute("SELECT COUNT(*) FROM posts WHERE status='video_ask' AND video_state='asked'").fetchone()[0]

    def videos_approved(self, limit: int = 5) -> list:
        return self.c.execute("SELECT * FROM posts WHERE status='video_ask' AND video_state='yes' ORDER BY id LIMIT ?",
                              (limit,)).fetchall()

    def update_post(self, pid: int, **fields) -> None:
        self._update("posts", "id", pid, fields)

    def dup_pool(self) -> list:
        """Посты с вектором, против которых ищем дубли: в очереди, разобранные и отклонённые."""
        return self.c.execute("SELECT id, emb FROM posts WHERE emb IS NOT NULL AND status IN "
                              "('candidate','drafted','written','rejected')").fetchall()

    def bump_attempts(self, pid: int) -> int:
        self.c.execute("UPDATE posts SET attempts=attempts+1 WHERE id=?", (pid,))
        return self.get_post(pid)["attempts"]

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.c.execute("SELECT status, COUNT(*) FROM posts GROUP BY status")}

    def explore_by_keywords(self, keywords, limit: int) -> list:
        """Посты вне тем, где есть слова из целей Некита (FTS5, слово по префиксу, порядок bm25; идея teledigest).
        Индекс временный, на одну ночь: без триггеров нечему рассинхронизироваться."""
        terms = [re.sub(r"[^\w ]", " ", k.lower()).strip() for k in keywords]
        q = " OR ".join(f'"{t}"*' if " " not in t else f'"{t}"' for t in terms if t)
        if not q or limit <= 0:
            return []
        self.c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.ex USING fts5(pid UNINDEXED, body)")
        self.c.execute("DELETE FROM temp.ex")
        self.c.execute("INSERT INTO temp.ex SELECT id, COALESCE(text, '') || ' ' || COALESCE(extra_text, '') "
                       "FROM posts WHERE status='explore'")
        return self.c.execute("SELECT p.* FROM temp.ex JOIN posts p ON p.id=ex.pid WHERE ex MATCH ? "
                              "ORDER BY bm25(ex), p.usefulness DESC LIMIT ?", (q, limit)).fetchall()

    def night_queue(self, explore_quota: int, section_cap: int = 0, keywords=()) -> list:
        """Кандидаты: свежие live первыми, дальше похожесть плюс охват (у сюжета есть дубли из других каналов,
        +0.03 за каждый, до трёх; идея nyan ranker.py). section_cap: не больше N постов одного раздела за ночь,
        остальные ждут следующей, иначе «ИИ работа» съедала почти весь лимит."""
        cand = self.c.execute(
            "SELECT p.*, COALESCE(d.n, 0) AS dups FROM posts p LEFT JOIN "
            "(SELECT reason, COUNT(*) AS n FROM posts WHERE status='duplicate' GROUP BY reason) d ON d.reason='дубль #' || p.id "
            "WHERE p.status='candidate' ORDER BY (p.source='live') DESC, p.score + 0.03 * MIN(3, COALESCE(d.n, 0)) DESC, p.id").fetchall()
        if section_cap:
            per: dict[str, int] = {}
            kept = []
            for r in cand:
                sec = (r["target"] or "").split("/")[0]
                per[sec] = per.get(sec, 0) + 1
                if per[sec] <= section_cap:
                    kept.append(r)
            cand = kept
        expl = list(self.explore_by_keywords(keywords, explore_quota)) if keywords else []
        seen = {r["id"] for r in expl}
        expl += [r for r in self.c.execute("SELECT * FROM posts WHERE status='explore' ORDER BY usefulness DESC, id LIMIT ?",
                                           (explore_quota,)) if r["id"] not in seen][: explore_quota - len(expl)]
        return list(cand) + list(expl)

    def pilot_sample(self, n: int) -> list:
        return self.c.execute(
            "SELECT * FROM posts WHERE score IS NOT NULL AND id NOT IN (SELECT post_id FROM labels) "
            "ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()

    # --- channels
    def max_msg_id(self, chat_id: int) -> int:
        return self.c.execute("SELECT COALESCE(MAX(msg_id), 0) FROM posts WHERE chat_id=?", (chat_id,)).fetchone()[0]

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
    def add_label(self, pid: int, useful: bool, *, source: str = "pilot") -> None:
        """Ответ Некита. Повторный ответ меняет вердикт, но не стирает уже записанную причину."""
        self.c.execute(
            "INSERT INTO labels(post_id, useful, source, ts) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(post_id) DO UPDATE SET useful=excluded.useful, source=excluded.source, ts=excluded.ts",
            (pid, int(useful), source, _now()))

    def set_label_reason(self, pid: int, reason: str) -> None:
        self.c.execute("UPDATE labels SET reason=? WHERE post_id=?", (reason, pid))

    def backfill_video_labels(self) -> int:
        """Ответы на видео-вопросы до появления меток лежали только в video_state: достаём их, не трогая готовые метки."""
        cur = self.c.execute(
            "INSERT OR IGNORE INTO labels(post_id, useful, source, ts) "
            "SELECT id, CASE WHEN video_state='no' THEN 0 ELSE 1 END, 'video', ? FROM posts "
            "WHERE video_state IN ('yes','done','no')", (_now(),))
        return cur.rowcount

    def labeled(self) -> list:
        return self.c.execute("SELECT p.score, p.section_score, l.useful FROM labels l "
                              "JOIN posts p ON p.id = l.post_id").fetchall()

    # --- метаданные файла, темы
    def save_meta(self, pid: int, doc_id: int | None, size: int | None, dur: float | None) -> None:
        self.c.execute("INSERT OR REPLACE INTO post_meta(post_id, doc_id, size, dur) VALUES(?, ?, ?, ?)",
                       (pid, doc_id, size, dur))

    def get_meta(self, pid: int):
        return self.c.execute("SELECT * FROM post_meta WHERE post_id=?", (pid,)).fetchone()

    def queue_without_meta(self, limit: int) -> list:
        return self.c.execute(
            "SELECT p.* FROM posts p LEFT JOIN post_meta m ON m.post_id=p.id "
            "WHERE p.status='video_ask' AND p.video_state='ask' AND m.post_id IS NULL ORDER BY p.id LIMIT ?",
            (limit,)).fetchall()

    def set_topic(self, pid: int, topic: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO topics(post_id, topic) VALUES(?, ?)", (pid, topic))

    def topic_of(self, pid: int) -> str | None:
        r = self.c.execute("SELECT topic FROM topics WHERE post_id=?", (pid,)).fetchone()
        return r[0] if r else None

    def queue_without_topic(self, limit: int) -> list:
        return self.c.execute(
            "SELECT p.* FROM posts p LEFT JOIN topics t ON t.post_id=p.id "
            "WHERE p.status='video_ask' AND p.video_state='ask' AND t.post_id IS NULL ORDER BY p.id LIMIT ?",
            (limit,)).fetchall()

    def topic_names(self, limit: int = 40) -> list[str]:
        return [r[0] for r in self.c.execute(
            "SELECT topic FROM topics GROUP BY topic ORDER BY COUNT(*) DESC, topic LIMIT ?", (limit,))]

    def topic_overview(self) -> list:
        """Темы очереди видео-вопросов: сколько постов и сколько разных файлов (повторы по document id считаются один раз)."""
        return self.c.execute(
            "SELECT t.topic AS topic, COUNT(*) AS n, COUNT(DISTINCT COALESCE(m.doc_id, -p.id)) AS files "
            "FROM posts p JOIN topics t ON t.post_id=p.id LEFT JOIN post_meta m ON m.post_id=p.id "
            "WHERE p.status='video_ask' AND p.video_state='ask' GROUP BY t.topic ORDER BY n DESC, t.topic").fetchall()

    # --- состояние тем
    def topic_state_id(self, topic: str) -> int | None:
        r = self.c.execute("SELECT id FROM topic_state WHERE topic=?", (topic,)).fetchone()
        return r[0] if r else None

    def ensure_topic_state(self, topic: str) -> int:
        self.c.execute("INSERT OR IGNORE INTO topic_state(topic) VALUES(?)", (topic,))
        return self.topic_state_id(topic)

    def topic_state_of(self, topic: str) -> str | None:
        r = self.c.execute("SELECT state FROM topic_state WHERE topic=?", (topic,)).fetchone()
        return r[0] if r else None

    def topic_by_id(self, tid: int):
        return self.c.execute("SELECT * FROM topic_state WHERE id=?", (tid,)).fetchone()

    def set_topic_state(self, tid: int, state: str, **fields) -> None:
        self._update("topic_state", "id", tid, {"state": state, **fields})

    def topic_queue(self, topic: str) -> list:
        return self.c.execute(
            "SELECT p.* FROM posts p JOIN topics t ON t.post_id=p.id WHERE t.topic=? AND p.status='video_ask' "
            "AND p.video_state='ask' ORDER BY p.score DESC, p.id", (topic,)).fetchall()

    def unmanaged_video_asks(self, limit: int, min_size: int) -> list:
        """Вопросы по одному: только посты с темой, по которой в очереди меньше min_size видео и карточки темы не было."""
        return self.c.execute(
            "SELECT p.* FROM posts p JOIN topics t ON t.post_id=p.id "
            "WHERE p.status='video_ask' AND p.video_state='ask' "
            "AND NOT EXISTS (SELECT 1 FROM topic_state ts WHERE ts.topic=t.topic) "
            "AND (SELECT COUNT(*) FROM posts p2 JOIN topics t2 ON t2.post_id=p2.id WHERE t2.topic=t.topic "
            "     AND p2.status='video_ask' AND p2.video_state='ask') < ? "
            "ORDER BY p.score DESC, p.id LIMIT ?", (min_size, limit)).fetchall()

    def kv_prefix(self, prefix: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.c.execute("SELECT key, value FROM kv WHERE key LIKE ? AND value != ''",
                                                     (prefix + "%",))]

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
