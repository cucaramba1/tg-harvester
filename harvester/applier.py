"""Ноут: забрать пакеты записи с сервера, применить Writer'ом, отправить ответы и обновить зеркало vault на сервере.

Всегда инициирует ноут (ssh/rsync наружу), сервер к ноуту не подключается. Запуск: `python -m harvester applier`
(launchd раз в 5 минут; если сервер недоступен или ноут спал, следующий запуск просто доберёт накопившееся)."""
from __future__ import annotations

import logging
import shlex
import subprocess

from .config import Settings
from .db import DB
from .packets import apply_inbox

log = logging.getLogger(__name__)

MIRROR_FILTER = ["--delete", "--prune-empty-dirs", "--exclude=.obsidian/", "--exclude=.trash/", "--exclude=.git/",
                 "--include=*/", "--include=*.md", "--include=*.canvas", "--exclude=*"]


def mirror_names(s: Settings) -> list[tuple[str, object]]:
    """Корни vault -> имена папок зеркала на сервере (main, extra0, ...). Имена без пробелов: путь режется удалённой оболочкой."""
    return [("main" if i == 0 else f"extra{i - 1}", root) for i, root in enumerate(s.vaults)]


def run_applier(s: Settings, db: DB, run=subprocess.run) -> dict:
    rep: dict = {"applied": [], "skipped": None, "mirror": []}
    if not s.remote_host:
        rep["skipped"] = "REMOTE_HOST не задан"
        return rep
    ssh = f"ssh -i {shlex.quote(s.remote_key)} -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10"
    host, root = s.remote_host, s.remote_root

    def rsync(*args: str):
        return run(["rsync", "-a", "-e", ssh, *args], capture_output=True, text=True)

    r = run([*shlex.split(ssh), host, f"mkdir -p {root}/data/acks {root}/data/outbox vault-mirror"], capture_output=True, text=True)
    if r.returncode != 0:
        rep["skipped"] = f"сервер недоступен: {r.stderr.strip()[-120:]}"
        return rep
    s.inbox_dir.mkdir(parents=True, exist_ok=True)
    r = rsync("--delete", "--exclude=*.tmp", f"{host}:{root}/data/outbox/", f"{s.inbox_dir}/")
    if r.returncode != 0:
        rep["skipped"] = f"не забрал пакеты (rsync {r.returncode}): {r.stderr.strip()[-120:]}"
        return rep
    rep["applied"] = apply_inbox(s, db)
    if s.acks_out_dir.exists() and any(s.acks_out_dir.glob("*.json")):
        r = rsync("--remove-source-files", f"{s.acks_out_dir}/", f"{host}:{root}/data/acks/")
        if r.returncode != 0:  # ответы остаются на диске, уйдут в следующий раз; записанное повторно не запишется
            log.error("ответы не ушли (rsync %s): %s", r.returncode, r.stderr.strip()[-200:])
    for name, vroot in mirror_names(s):
        if not vroot.is_dir():
            continue
        r = rsync(*MIRROR_FILTER, f"{vroot}/", f"{host}:vault-mirror/{name}/")
        rep["mirror"].append((name, r.returncode))
        if r.returncode != 0:
            log.error("зеркало %s: rsync %s %s", name, r.returncode, r.stderr.strip()[-200:])
    return rep
