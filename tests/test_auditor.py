import asyncio
import json

from harvester.auditor import run_audit
from harvester.brain import BrainError
from tests.helpers import FakeClaude


def test_audit_records_doubts_for_active_blocks_only(s, db):
    (s.vault / "n.md").write_text("# N\n\nтекст\n\n## Из Telegram\n\n- блок\n", "utf-8")
    b1 = db.add_block(1, "n.md", "append", 15, 40, "x", {"text": "\n## Из Telegram\n\n- блок\n", "line": "- блок\n"})
    b2 = db.add_block(2, "n.md", "append", 15, 40, "x", {"text": "z", "line": "z"})
    db.mark_rolled_back(b2)
    b3 = db.add_block(3, "gone.md", "append", 0, 1, "x", {"text": "q", "line": "q"})
    fake = FakeClaude([lambda p: {"items": [{"block_id": b1, "verdict": "doubt", "reason": "не по теме"},
                                            {"block_id": 999, "verdict": "doubt", "reason": "чужой"}]}])
    rep = asyncio.run(run_audit(s, db, claude=fake))
    prompt = fake.calls[0][0]
    assert f"block_id {b1} " in prompt and f"block_id {b2} " not in prompt and f"block_id {b3} " not in prompt
    assert "- блок" in prompt and "текст" in prompt
    audits = db.audits("new")
    assert [(a["block_id"], a["reason"]) for a in audits] == [(b1, "не по теме")]
    assert (rep["checked"], rep["doubt"], rep["calls"]) == (1, 1, 1)
    assert json.loads(db.kv_get("audit_report"))["doubt"] == 1


def test_audit_stops_on_error(s, db):
    (s.vault / "n.md").write_text("x", "utf-8")
    db.add_block(1, "n.md", "new", 0, 1, "x", {"item": "[[n]]"})
    rep = asyncio.run(run_audit(s, db, claude=FakeClaude([BrainError("usage limit")])))
    assert rep["aborted"] and db.audits("new") == []
