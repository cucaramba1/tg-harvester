import json
from datetime import date

import pytest

from harvester.files import prune_snapshots
from harvester.writer import Writer, WriteError, post_link
from tests.helpers import CHAT_A, make_draft

NOTE = "---\ntags: [x]\n---\n# Промпты\n\nмой текст\n"
REL = "ИИ работа/Промпты.md"


def note(s, text=NOTE, rel=REL):
    p = s.vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    return p


def test_append_adds_heading_and_keeps_original_bytes(s, db):
    p = note(s)
    did = make_draft(db, 1, "append", REL)
    Writer(s, db).apply(did)
    out = p.read_text("utf-8")
    assert out == NOTE + f"\n## Из Telegram\n\n- суть поста ([пост]({post_link(CHAT_A, 1)}), 2026-10-01)\n"
    assert db.get_draft(did)["status"] == "written"
    assert db.get_post(db.get_draft(did)["post_id"])["status"] == "written"
    snap = s.snapshots_dir / date.today().isoformat() / REL
    assert snap.read_text("utf-8") == NOTE


def test_append_multiline_content_is_indented(s, db):
    p = note(s)
    Writer(s, db).apply(make_draft(db, 1, "append", REL, content="первая\nвторая"))
    assert p.read_text("utf-8").endswith("- первая\n  вторая\n  ([пост](https://t.me/c/1234/1), 2026-10-01)\n")


def test_append_goes_into_existing_heading_before_next_section(s, db):
    text = "# N\n\n## Из Telegram\n\n- старое\n\n## Ещё\n\nхвост\n"
    p = note(s, text)
    Writer(s, db).apply(make_draft(db, 1, "append", REL, content="новое"))
    out = p.read_text("utf-8")
    assert out.startswith("# N\n\n## Из Telegram\n\n- старое\n\n- новое (")
    assert out.endswith(")\n\n## Ещё\n\nхвост\n")
    assert out.count("## Из Telegram") == 1


def test_rollback_restores_exact_bytes(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    assert w.rollback(bid) == "ok"
    assert p.read_text("utf-8") == NOTE
    assert w.rollback(bid) == "already"
    assert db.get_draft(db.get_block(bid)["draft_id"])["status"] == "rolled_back"


def test_rollback_survives_edit_above_block(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    p.write_text("строка сверху\n" + p.read_text("utf-8"), "utf-8")
    assert w.rollback(bid) == "ok"
    assert p.read_text("utf-8") == "строка сверху\n" + NOTE


def test_rollback_refuses_when_block_was_edited(s, db):
    p = note(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 1, "append", REL))
    p.write_text(p.read_text("utf-8").replace("суть поста", "суть поста, поправил руками"), "utf-8")
    assert w.rollback(bid) == "changed"
    assert "поправил руками" in p.read_text("utf-8")


def test_rollback_first_of_two_keeps_heading_for_second(s, db):
    p = note(s)
    w = Writer(s, db)
    b1 = w.apply(make_draft(db, 1, "append", REL, content="первое"))
    b2 = w.apply(make_draft(db, 2, "append", REL, content="второе"))
    assert w.rollback(b1) == "ok"
    assert p.read_text("utf-8").startswith(NOTE + "\n## Из Telegram\n\n- второе (")
    assert w.rollback(b2) == "ok"
    assert p.read_text("utf-8") == NOTE + "\n## Из Telegram\n\n"


def test_missing_note_raises_and_draft_stays(s, db):
    did = make_draft(db, 1, "append", "ИИ работа/Нет.md")
    with pytest.raises(WriteError):
        Writer(s, db).apply(did)
    assert db.get_draft(did)["status"] == "drafted"


def test_target_outside_vault_is_rejected(s, db):
    (s.root / "evil.md").write_text("x", "utf-8")
    with pytest.raises(WriteError):
        Writer(s, db).apply(make_draft(db, 1, "append", "../evil.md"))
    with pytest.raises(WriteError):
        Writer(s, db).apply(make_draft(db, 2, "new", "..", title="x"))
    assert (s.root / "evil.md").read_text("utf-8") == "x"


def _music(s):
    sec = s.vault / "Музыка"
    sec.mkdir()
    (sec / "Аккорды.md").write_text("# Аккорды\n", "utf-8")
    moc = sec / "Музыка — карта заметок.md"
    moc.write_text("# Музыка — карта заметок\n", "utf-8")
    cvp = s.vault / "Музыка.canvas"
    cvp.write_text('{"nodes": [], "edges": []}', "utf-8")
    return sec, moc, cvp


def test_new_note_with_moc_canvas_and_rollback(s, db):
    sec, moc, cvp = _music(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 7, "new", "Музыка", content="Как свести трек", title="Основы сведения",
                             top3=[("Музыка/Аккорды.md", 0.7)]))
    n = sec / "Основы сведения.md"
    text = n.read_text("utf-8")
    assert "tags: [telegram]" in text and "# Основы сведения" in text and "Как свести трек" in text
    assert "[[Музыка/Аккорды|Аккорды]]" in text and post_link(CHAT_A, 7) in text
    assert "[[Музыка/Основы сведения|Основы сведения]]" in moc.read_text("utf-8")
    cv = json.loads(cvp.read_text("utf-8"))
    assert any(x.get("file") == "Музыка/Основы сведения.md" for x in cv["nodes"])
    assert w.rollback(bid) == "ok"
    assert not n.exists()
    assert moc.read_text("utf-8") == "# Музыка — карта заметок\n"
    assert json.loads(cvp.read_text("utf-8"))["nodes"] == []


def test_new_note_edited_by_hand_is_not_rolled_back(s, db):
    sec, _, _ = _music(s)
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 7, "new", "Музыка", title="Сведение"))
    (sec / "Сведение.md").write_text("переписал", "utf-8")
    assert w.rollback(bid) == "changed"
    assert (sec / "Сведение.md").exists()


def test_keep_image_copies_attachment_and_rollback_removes_it(s, db, tmp_path):
    p = note(s)
    img = tmp_path / "keep" / "pic.jpg"
    img.parent.mkdir()
    img.write_bytes(b"jpg")
    w = Writer(s, db)
    bid = w.apply(make_draft(db, 9, "append", REL, keep_image=True, images=[str(img)]))
    att = s.vault / "Вложения/Telegram/tg-1234-9.jpg"
    assert att.read_bytes() == b"jpg"
    assert "![[Вложения/Telegram/tg-1234-9.jpg]]" in p.read_text("utf-8")
    assert w.rollback(bid) == "ok"
    assert not att.exists() and p.read_text("utf-8") == NOTE


def test_prune_snapshots_drops_old_days(s):
    old = s.snapshots_dir / "2026-01-01"
    new = s.snapshots_dir / "2026-10-09"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    assert prune_snapshots(s, today=date(2026, 10, 10)) == 1
    assert not old.exists() and new.exists()
