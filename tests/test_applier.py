import subprocess

from harvester.applier import MIRROR_FILTER, mirror_names, run_applier
from harvester.db import DB
from tests.helpers import make_settings


def setup(tmp_path, **over):
    s = make_settings(tmp_path, remote_host="u@h", remote_key="/k", **over)
    return s, DB(s.db_path)


class Runner:
    def __init__(self, fail_first=False):
        self.calls, self.fail_first = [], fail_first

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        rc = 255 if self.fail_first and len(self.calls) == 1 else 0
        return subprocess.CompletedProcess(cmd, rc, "", "no route" if rc else "")


def test_no_remote_host_is_skipped(tmp_path):
    s = make_settings(tmp_path)
    r = Runner()
    assert run_applier(s, DB(s.db_path), r)["skipped"] == "REMOTE_HOST не задан" and r.calls == []


def test_unreachable_server_stops_quietly(tmp_path):
    s, db = setup(tmp_path)
    r = Runner(fail_first=True)
    rep = run_applier(s, db, r)
    assert "сервер недоступен" in rep["skipped"] and len(r.calls) == 1          # дальше не идём: ноут офлайн или сервер лёг


def test_order_pull_then_acks_then_mirror(tmp_path):
    s, db = setup(tmp_path)
    s.acks_out_dir.mkdir(parents=True)
    (s.acks_out_dir / "x.json").write_text("{}")
    r = Runner()
    rep = run_applier(s, db, r)
    flat = [" ".join(c) for c in r.calls]
    assert rep["skipped"] is None and rep["mirror"] == [("main", 0)]
    assert "mkdir -p" in flat[0] and "--delete" in flat[1] and "u@h:tg-harvester/data/outbox/" in flat[1]
    assert "--remove-source-files" in flat[2] and flat[2].endswith("u@h:tg-harvester/data/acks/")
    assert "u@h:vault-mirror/main/" in flat[3] and all(f in r.calls[3] for f in MIRROR_FILTER)


def test_mirror_names_have_no_spaces(tmp_path):
    s, _ = setup(tmp_path, extra_vaults=(tmp_path / "Obsidian Досуг",))
    assert [n for n, _ in mirror_names(s)] == ["main", "extra0"]
