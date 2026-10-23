import asyncio
import json

from harvester.brain import BrainError, run_night
from tests.helpers import CHAT_A, FakeClaude

REL = "ИИ работа/Промпты.md"


def setup_vault(s):
    for sec in ("ИИ работа", "Музыка"):
        (s.vault / sec).mkdir()
        (s.vault / f"{sec}.canvas").write_text('{"nodes":[],"edges":[]}', "utf-8")
    (s.vault / REL).write_text("# Промпты\n", "utf-8")


def cand(db, msg, *, source="history", score=0.7, status="candidate", extra="", images=(), usefulness=0.5):
    pid = db.add_post(CHAT_A, msg, None, "2026-10-01T00:00:00+00:00", f"пост {msg} про промпты", None, source)
    db.update_post(pid, status=status, score=score, extra_text=extra, images=list(images), top3=[[REL, 0.7]],
                   mode="append", target=REL, usefulness=usefulness)
    return pid


def answer(*items):
    return {"items": list(items)}


def item(pid, mode="append", target=REL, title=None, content="суть", reason="ок", keep=False):
    return {"post_id": pid, "mode": mode, "target": target, "title": title, "content": content,
            "reason": reason, "keep_image": keep}


def night(s, db, fake):
    return asyncio.run(run_night(s, db, claude=fake))


def test_drafts_skips_and_missing(s, db):
    setup_vault(s)
    a, b, c = cand(db, 1), cand(db, 2), cand(db, 3)
    fake = FakeClaude([lambda p: answer(item(a), item(b, mode="skip", reason="новость")), answer()])
    rep = night(s, db, fake)
    assert db.get_post(a)["status"] == "drafted"
    assert db.get_post(b)["status"] == "rejected" and "новость" in db.get_post(b)["reason"]
    assert db.get_post(c)["status"] == "candidate" and db.get_post(c)["attempts"] == 1
    d = db.drafts("drafted")[0]
    assert (d["mode"], d["target"], d["content"]) == ("append", REL, "суть")
    alts = json.loads(d["alts"])
    assert ["append", REL] in alts and ["new", "ИИ работа"] in alts
    assert (rep["drafted"], rep["skipped"], rep["calls"]) == (1, 1, 2)
    assert json.loads(db.kv_get("night_report"))["drafted"] == 1


def test_call_limit_and_retro_multiplier(s, db):
    setup_vault(s)
    for i in range(1, 11):
        cand(db, i)
    fake = FakeClaude([answer()] * 10)
    rep = night(s, db, fake)
    assert rep["limit"] == 3 and len(fake.calls) == 3
    db.save_channel(-1, read_mark=0, new_cursor=0, retro_cursor=1, retro_until=None, phase="retro")
    fake2 = FakeClaude([answer()] * 10)
    rep2 = night(s, db, fake2)
    assert rep2["limit"] == 6 and len(fake2.calls) == 5  # 10 постов по 2 = 5 батчей, в лимит влезли


def test_usage_limit_aborts_night_without_penalty(s, db):
    setup_vault(s)
    a = cand(db, 1)
    fake = FakeClaude([BrainError("claude: You've hit your usage limit")])
    rep = night(s, db, fake)
    assert rep["aborted"] and len(fake.calls) == 1
    p = db.get_post(a)
    assert (p["status"], p["attempts"]) == ("candidate", 0)
    assert any("Ночь прервана" in t for t in db.pop_alerts())


def test_usage_limit_switches_to_router_without_goals(s, db):
    import dataclasses
    setup_vault(s)
    (s.vault / s.goals_note).parent.mkdir(parents=True, exist_ok=True)
    (s.vault / s.goals_note).write_text("СЕКРЕТНАЯ ЦЕЛЬ", "utf-8")
    a = cand(db, 1)
    claude = FakeClaude([BrainError("claude: You've hit your usage limit")])
    router = FakeClaude([lambda p: answer(item(a))])
    s2 = dataclasses.replace(s, router_fallback=True)
    rep = asyncio.run(run_night(s2, db, claude=claude, router=router))
    assert rep["aborted"] is None and "fallback" in rep
    assert db.get_post(a)["status"] == "drafted"
    assert len(claude.calls) == 1 and len(router.calls) == 1
    assert "СЕКРЕТНАЯ ЦЕЛЬ" not in router.calls[0][0]


def test_missing_claude_binary_aborts_night(s, db):
    setup_vault(s)
    a = cand(db, 1)
    rep = night(s, db, FakeClaude([BrainError("не смог запустить claude: No such file")]))
    assert rep["aborted"] and db.get_post(a)["attempts"] == 0


def test_other_error_is_retried_once(s, db):
    setup_vault(s)
    a = cand(db, 1)
    fake = FakeClaude([BrainError("claude не ответил за 10 с"), lambda p: answer(item(a))])
    night(s, db, fake)
    assert db.get_post(a)["status"] == "drafted" and len(fake.calls) == 2


def test_three_failed_nights_mark_error(s, db):
    setup_vault(s)
    a = cand(db, 1)
    for _ in range(3):
        night(s, db, FakeClaude([BrainError("сбой"), BrainError("сбой")]))
    assert db.get_post(a)["status"] == "error"


def test_bad_targets_fall_back_to_section(s, db):
    setup_vault(s)
    a, b = cand(db, 1), cand(db, 2)
    fake = FakeClaude([lambda p: answer(item(a, target="ИИ работа/Нет такой.md"),
                                        item(b, mode="new", target="../../etc", title="Заметка"))])
    night(s, db, fake)
    da, dbb = sorted(db.drafts("drafted"), key=lambda d: d["post_id"])
    assert (da["mode"], da["target"], da["title"]) == ("new", "ИИ работа", "суть")
    assert (dbb["mode"], dbb["target"], dbb["title"]) == ("new", "ИИ работа", "Заметка")


def test_live_first_and_explore_quota(s, db):
    setup_vault(s)
    old = cand(db, 1, score=0.9)
    live = cand(db, 2, score=0.65, source="live")
    e1 = cand(db, 3, status="explore", usefulness=0.9)
    e2 = cand(db, 4, status="explore", usefulness=0.8)
    e3 = cand(db, 5, status="explore", usefulness=0.1)
    fake = FakeClaude([answer()] * 5)
    night(s, db, fake)
    prompts = "\n".join(c[0] for c in fake.calls)
    assert prompts.index(f"post_id {live} ") < prompts.index(f"post_id {old} ")
    assert f"post_id {e1} " in prompts and f"post_id {e2} " in prompts and f"post_id {e3} " not in prompts


def test_images_only_without_ocr_and_capped(s, db, tmp_path):
    setup_vault(s)
    img1, img2 = tmp_path / "k1" / "a.jpg", tmp_path / "k2" / "b.jpg"
    for p in (img1, img2):
        p.parent.mkdir()
        p.write_bytes(b"x")
    cand(db, 1, images=[str(img1)])
    cand(db, 2, images=[str(img2)])
    cand(db, 3, images=[str(img1)], extra="текст с картинки")
    fake = FakeClaude([answer()] * 3)
    night(s, db, fake)
    prompt, kw = fake.calls[0]
    assert kw["tools"] == ("Read",) and str(img1) in prompt and str(img2) not in prompt
    assert kw["add_dirs"] == (str(img1.parent),)
    assert fake.calls[1][1]["tools"] == ()


def test_router_tail_continues_after_claude_limit_without_goals(s, db):
    import dataclasses
    setup_vault(s)
    (s.vault / s.goals_note).parent.mkdir(parents=True, exist_ok=True)
    (s.vault / s.goals_note).write_text("СЕКРЕТНАЯ ЦЕЛЬ", "utf-8")
    ids = [cand(db, i, score=0.9 - i / 100) for i in range(1, 9)]  # 8 постов, батчи по 2
    s2 = dataclasses.replace(s, max_calls_per_night=1, router_fallback=True, router_tail_batches=2)
    claude = FakeClaude([lambda p: answer(item(ids[0]), item(ids[1]))])
    router = FakeClaude([lambda p: answer(item(ids[2]), item(ids[3])), lambda p: answer(item(ids[4]), item(ids[5]))])
    rep = asyncio.run(run_night(s2, db, claude=claude, router=router))
    assert (len(claude.calls), len(router.calls)) == (1, 2) and rep["calls"] == 3
    assert rep["tail"] == 2 and rep["drafted"] == 6
    assert all("СЕКРЕТНАЯ ЦЕЛЬ" not in c[0] for c in router.calls) and "СЕКРЕТНАЯ ЦЕЛЬ" in claude.calls[0][0]
    assert db.get_post(ids[6])["status"] == "candidate"  # дальше хвоста не идём


def test_no_router_tail_when_disabled(s, db):
    import dataclasses
    setup_vault(s)
    for i in range(1, 7):
        cand(db, i)
    s2 = dataclasses.replace(s, max_calls_per_night=1, router_fallback=True, router_tail_batches=0)
    claude, router = FakeClaude([answer()]), FakeClaude([])
    asyncio.run(run_night(s2, db, claude=claude, router=router))
    assert len(claude.calls) == 1 and len(router.calls) == 0


def test_router_tail_failure_stops_tail_and_never_penalizes_posts(s, db):
    import dataclasses
    setup_vault(s)
    ids = [cand(db, i, score=0.9 - i / 100) for i in range(1, 7)]
    s2 = dataclasses.replace(s, max_calls_per_night=1, router_fallback=True, router_tail_batches=3)
    claude = FakeClaude([lambda p: answer(item(ids[0]), item(ids[1]))])
    router = FakeClaude([BrainError("роутер недоступен: HTTP Error 502"), answer()])
    rep = asyncio.run(run_night(s2, db, claude=claude, router=router))
    assert len(router.calls) == 1 and rep["tail_error"] and rep["aborted"] is None
    for pid in ids[2:]:
        p = db.get_post(pid)
        assert (p["status"], p["attempts"]) == ("candidate", 0)


def test_router_tail_garbage_answer_does_not_burn_attempts(s, db):
    import dataclasses
    setup_vault(s)
    ids = [cand(db, i, score=0.9 - i / 100) for i in range(1, 5)]
    s2 = dataclasses.replace(s, max_calls_per_night=1, router_fallback=True, router_tail_batches=1)
    claude = FakeClaude([lambda p: answer(item(ids[0]), item(ids[1]))])
    router = FakeClaude([answer()])  # роутер ответил пустотой
    asyncio.run(run_night(s2, db, claude=claude, router=router))
    assert db.get_post(ids[2])["attempts"] == 0 and db.get_post(ids[3])["attempts"] == 0


class _Resp:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return json.dumps({"choices": [{"message": {"content": self.content}}]}).encode()


def test_run_router_tries_next_model_when_first_is_rate_limited(s, monkeypatch):
    import urllib.error
    import urllib.request

    from harvester import brain
    monkeypatch.setenv("FREELLM_KEY", "k")
    monkeypatch.setattr(brain, "ROUTER_MODELS", ("first", "auto"))
    asked = []

    def fake_open(rq, timeout=0):
        model = json.loads(rq.data)["model"]
        asked.append(model)
        if model == "first":
            raise urllib.error.HTTPError(rq.full_url, 502, "Bad Gateway", {}, None)
        return _Resp('{"items": []}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    out = asyncio.run(brain.run_router(s, "p", system="s", schema={"items": []}, timeout=5))
    assert out == {"items": []} and asked == ["first", "auto"]


def test_run_router_raises_brain_error_when_all_models_fail(s, monkeypatch):
    import urllib.error
    import urllib.request

    import pytest

    from harvester import brain
    monkeypatch.setenv("FREELLM_KEY", "k")
    monkeypatch.setattr(brain, "ROUTER_MODELS", ("a", "b"))
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda rq, timeout=0: (_ for _ in ()).throw(urllib.error.HTTPError(rq.full_url, 502, "x", {}, None)))
    with pytest.raises(BrainError, match="роутер недоступен"):
        asyncio.run(brain.run_router(s, "p", system="s", timeout=5))
