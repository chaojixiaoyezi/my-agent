"""新增网关锁目录沿原清扫合同，只回收超龄且未持有的孤儿锁。"""

from __future__ import annotations

import os
import time

import pytest

from agent_py_agent.agent.gateway_parts.io import (
    cleanup_orphan_lock_files,
    gateway_lock_sidecar_dirs,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


@pytest.mark.parametrize("name", ["input_receipts", "client_outbox", "control_operations"])
def test_registered_directory_cleans_only_old_unlocked_orphans(tmp_path, name):
    fcntl = pytest.importorskip("fcntl")
    paths = gateway_paths_from_root(tmp_path / "gateway")
    directory = paths.root / name
    directory.mkdir(parents=True)
    old_orphan = directory / "a.json.lock"
    data = directory / "b.json"
    data_lock = directory / "b.json.lock"
    young_orphan = directory / "c.json.lock"
    held_orphan = directory / "d.json.lock"
    data.write_text("{}", encoding="utf-8")
    now = time.time()
    for path in (old_orphan, data, data_lock, young_orphan, held_orphan):
        path.touch()
        stamp = now - (3600 if path == young_orphan else 25 * 3600)
        os.utime(path, (stamp, stamp))
    held_inode = held_orphan.stat().st_ino

    with held_orphan.open("a+", encoding="utf-8") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            removed = sum(cleanup_orphan_lock_files(path) for path in gateway_lock_sidecar_dirs(paths))
            assert data.read_text(encoding="utf-8") == "{}"
            assert data_lock.exists() and young_orphan.exists()
            assert held_orphan.stat().st_ino == held_inode
            assert removed == 1
            assert not old_orphan.exists()
            assert sum(cleanup_orphan_lock_files(path) for path in gateway_lock_sidecar_dirs(paths)) == 0
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)

    # 原活锁释放后才可清理，同一目录的年轻锁和数据伴生锁继续保留。
    assert sum(cleanup_orphan_lock_files(path) for path in gateway_lock_sidecar_dirs(paths)) == 1
    assert not held_orphan.exists()
    assert data_lock.exists() and young_orphan.exists()
