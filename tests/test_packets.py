"""Круг «сервер -> пакет -> ноут (настоящий Writer) -> ack -> сервер» без сети: две папки, две базы."""
import json
import shutil

from harvester.db import DB
from harvester.packets import PacketWriter, apply_inbox, make_writer, read_acks
from harvester.writer import Writer, WriteError
from tests.helpers import make_draft, make_settings

import pytest

NOTE = "---\ntags: [x]\n---\n# Промпты\n\nмой текст\n"
REL = "ИИ работа/Промпты.md"


class Pair:
    """Сервер и ноут: у каждого свои папка, база и (у ноута) настоящий vault."""

    def __init__(self, tmp_path):
        (tmp_path / "srv").mkdir()
        (tmp_path / "lap").mkdir()
        self.srv_s = make_settings(tmp_path / "srv", apply_mode="packet")
        self.lap_s = make_settings(tmp_path / "lap")
        self.srv, self.lap = DB(self.srv_s.db_path), DB(self.lap_s.db_path)
        p = self.lap_s.vault / REL
        p.parent.mkdir(parents=True)
        p.write_text(NOTE, "utf-8")
        self.note = p

    def draft(self, msg_id=1, **kw):
        return make_draft(self.srv, msg_id, "append", REL, **kw)

    def to_laptop(self):  # rsync вместо сети: outbox сервера -> inbox ноута (с --delete)
        shutil.rmtree(self.lap_s.inbox_dir, ignore_errors=True)
        if self.srv_s.outbox_dir.exists():
            shutil.copytree(self.srv_s.outbox_dir, self.lap_s.inbox_dir)
        return apply_inbox(self.lap_s, self.lap)

    def to_server(self):
        self.srv_s.acks_dir.mkdir(parents=True, exist_ok=True)
        for f in self.lap_s.acks_out_dir.glob("*.json"):
            shutil.move(f, self.srv_s.acks_dir / f.name)
        return read_acks(self.srv_s, self.srv)


def test_make_writer_by_mode(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert isinstance(make_writer(make_settings(tmp_path / "a", apply_mode="packet"), None), PacketWriter)
    assert isinstance(make_writer(make_settings(tmp_path / "b"), None), Writer)


def test_apply_roundtrip_writes_on_laptop_and_updates_server(tmp_path):
    pr = Pair(tmp_path)
    did = pr.draft()
    assert PacketWriter(pr.srv_s, pr.srv).apply(did) is None
    assert pr.srv.get_draft(did)["status"] == "queued"
    assert pr.note.read_text("utf-8") == NOTE                       # пока ноут не ответил, vault не тронут
    assert pr.to_laptop() == [f"apply-{did}"]
    assert "суть поста" in pr.note.read_text("utf-8")               # настоящий Writer записал на ноуте
    ev = pr.to_server()
    assert [e["type"] for e in ev] == ["written"] and ev[0]["path"] == REL
    assert pr.srv.get_draft(did)["status"] == "written"
    assert pr.srv.get_block(ev[0]["block_id"]) is not None
    assert not (pr.srv_s.outbox_dir / f"apply-{did}.json").exists()  # запрос снят после ответа


def test_redelivery_does_not_write_twice(tmp_path):
    pr = Pair(tmp_path)
    did = pr.draft()
    PacketWriter(pr.srv_s, pr.srv).apply(did)
    pr.to_laptop()
    once = pr.note.read_text("utf-8")
    pr.to_laptop()                                                   # ноут забрал тот же пакет второй раз (ack не дошёл)
    assert pr.note.read_text("utf-8") == once and once.count("суть поста") == 1
    assert len(pr.to_server()) == 1                                  # и сервер видит ровно одно событие
    pr.to_laptop()
    assert pr.to_server() == []                                      # повторный ack: событий нет, карточка не дублируется


def test_failure_returns_draft_to_sent(tmp_path):
    pr = Pair(tmp_path)
    did = make_draft(pr.srv, 1, "append", "Нет такой/заметки.md")
    PacketWriter(pr.srv_s, pr.srv).apply(did)
    pr.to_laptop()
    ev = pr.to_server()
    assert ev[0]["type"] == "failed" and "заметки больше нет" in ev[0]["error"]
    assert pr.srv.get_draft(did)["status"] == "sent"                 # кнопки снова работают


def test_image_travels_with_packet(tmp_path):
    pr = Pair(tmp_path)
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"jpg")
    did = pr.draft(keep_image=True, images=[str(img)])
    PacketWriter(pr.srv_s, pr.srv).apply(did)
    pr.to_laptop()
    out = pr.to_server()
    assert out[0]["type"] == "written"
    assert (pr.lap_s.vault / "Вложения/Telegram/tg-1234-1.jpg").read_bytes() == b"jpg"
    assert "![[Вложения/Telegram/tg-1234-1.jpg]]" in pr.note.read_text("utf-8")


def test_rollback_roundtrip(tmp_path):
    pr = Pair(tmp_path)
    did = pr.draft()
    w = PacketWriter(pr.srv_s, pr.srv)
    w.apply(did)
    pr.to_laptop()
    bid = pr.to_server()[0]["block_id"]
    assert w.rollback(bid) == "queued"
    pr.to_laptop()
    ev = pr.to_server()
    assert ev == [{"type": "rolled_back", "block_id": bid, "res": "ok", "error": ""}]
    assert pr.note.read_text("utf-8") == NOTE                        # байты вернулись
    assert pr.srv.get_block(bid)["rolled_back"] == 1 and pr.srv.get_draft(did)["status"] == "rolled_back"
    assert w.rollback(bid) == "already"


def test_second_apply_of_handled_draft_is_refused(tmp_path):
    pr = Pair(tmp_path)
    did = pr.draft()
    w = PacketWriter(pr.srv_s, pr.srv)
    w.apply(did)
    with pytest.raises(WriteError):
        w.apply(did)                                                 # статус queued: второй ✅ не создаёт второй пакет


def test_rollback_of_unknown_block_reports_error(tmp_path):
    pr = Pair(tmp_path)
    pr.srv_s.outbox_dir.mkdir(parents=True)
    (pr.srv_s.outbox_dir / "rollback-99.json").write_text(json.dumps({"key": "rollback-99", "kind": "rollback", "block_id": 99}))
    pr.to_laptop()
    ev = pr.to_server()
    assert ev[0]["res"] == "error" and "99" in ev[0]["error"]
