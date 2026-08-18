"""wake_legacy_migration 测试（#233 第 5 步：525 遗留 active 隔离台账）。

覆盖：隔离 legacy active（有 run 跳过）；有权威 run 跳过；幂等；revert 可回滚；
dry-run 零写；before/after 计数落账。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.wake_legacy_migration import (
    isolate_legacy_active_tasks,
    revert_legacy_migration,
)

NOW = 1_700_000_000.0
OWNER = "local/main"


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _real_task(repo, task_id):
    return repo.create_task(
        owner_id=OWNER, task_id=task_id, conversation_task_id=task_id, title="t"
    )


def test_isolates_legacy_active_tasks(repo, tmp_path):
    """无 run 无 policy 的 active 任务 → ORPHANED 台账行；有 run 的跳过。"""
    t_no_run = _real_task(repo, "legacy-no-run")["task_id"]
    t_with_run = _real_task(repo, "legacy-with-run")["task_id"]
    repo.record_run_creation(
        owner_id=OWNER, conversation_task_id=t_with_run, run_id=t_with_run, role="main"
    )
    counts = isolate_legacy_active_tasks(
        repo, owner_id=OWNER, owner_home=tmp_path / "home", now=NOW,
        migration_version="v1-test",
    )
    assert counts["isolated"] == 1
    assert counts["scanned"] == 2
    # t_no_run 隔离为 ORPHANED；t_with_run 不隔离
    m_no_run = repo.get_legacy_migration(t_no_run)
    assert m_no_run is not None and m_no_run["isolation_state"] == "ORPHANED"
    assert m_no_run["reason"] == "legacy_unbound_no_run_no_policy"
    assert m_no_run["migration_version"] == "v1-test"
    assert repo.get_legacy_migration(t_with_run) is None
    # tasks.status 不变（仍是 active）
    with repo._runtime_connection() as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE task_id = ?", (t_no_run,)
        ).fetchone()[0]
    assert status == "active"


def test_skips_task_with_authority_run(repo, tmp_path):
    t = _real_task(repo, "task-with-run")["task_id"]
    repo.record_run_creation(
        owner_id=OWNER, conversation_task_id=t, run_id=t, role="main"
    )
    counts = isolate_legacy_active_tasks(
        repo, owner_id=OWNER, owner_home=tmp_path / "home", now=NOW,
    )
    assert counts["isolated"] == 0
    assert repo.get_legacy_migration(t) is None


def test_idempotent(repo, tmp_path):
    t = _real_task(repo, "legacy-idem")["task_id"]
    c1 = isolate_legacy_active_tasks(
        repo, owner_id=OWNER, owner_home=tmp_path / "home", now=NOW,
        migration_version="v1-test",
    )
    c2 = isolate_legacy_active_tasks(
        repo, owner_id=OWNER, owner_home=tmp_path / "home", now=NOW + 100,
        migration_version="v1-test",
    )
    assert c1["isolated"] == 1
    assert c2["isolated"] == 0  # 已隔离跳过（幂等）
    with repo._runtime_connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM wake_legacy_migrations WHERE task_id = ?", (t,)
        ).fetchone()[0]
    assert n == 1


def test_reversible(repo, tmp_path):
    t = _real_task(repo, "legacy-rev")["task_id"]
    isolate_legacy_active_tasks(
        repo, owner_id=OWNER, owner_home=tmp_path / "home", now=NOW,
        migration_version="v1-test",
    )
    assert repo.get_legacy_migration(t) is not None
    removed = revert_legacy_migration(repo, migration_version="v1-test")
    assert removed == 1
    assert repo.get_legacy_migration(t) is None
    # tasks 完全不动
    with repo._runtime_connection() as conn:
        status = conn.execute(
            "SELECT status FROM tasks WHERE task_id = ?", (t,)
        ).fetchone()[0]
    assert status == "active"
    # 重跑可再隔离
    c2 = isolate_legacy_active_tasks(
        repo, owner_id=OWNER, owner_home=tmp_path / "home", now=NOW + 200,
        migration_version="v1-test",
    )
    assert c2["isolated"] == 1


def test_dry_run_no_writes(repo, tmp_path):
    t = _real_task(repo, "legacy-dry")["task_id"]
    counts = isolate_legacy_active_tasks(
        repo, owner_id=OWNER, owner_home=tmp_path / "home", now=NOW,
        migration_version="v1-test", dry_run=True,
    )
    assert counts["isolated"] == 1  # dry-run 只计数
    assert repo.get_legacy_migration(t) is None  # 不写台账


def test_ledgers_before_after_counts(repo, tmp_path):
    """run_legacy_migration 全编排：before/after 计数落 metadata。"""
    t1 = _real_task(repo, "legacy-ledger-1")["task_id"]
    t2 = _real_task(repo, "legacy-ledger-2")["task_id"]
    # t1 有 run → 跳过；t2 无 run → 隔离
    repo.record_run_creation(
        owner_id=OWNER, conversation_task_id=t1, run_id=t1, role="main"
    )
    from agent_py_agent.agent.wake_legacy_migration import run_legacy_migration

    # 构造 owners_dir 布局（legacy local/main —— base owner 所在，525 就在这）
    owners_dir = tmp_path / "owners"
    home_dir = owners_dir / "local" / "main"
    home_dir.mkdir(parents=True, exist_ok=True)
    # 把当前 repo 的 db 移到 owner home（模拟既有库）
    import shutil

    db_path = tmp_path / "home" / "runtime.db"
    shutil.copy(str(db_path), str(home_dir / "runtime.db"))
    summary = run_legacy_migration(owners_dir, migration_version="v1-test", now=NOW)
    assert summary["owners"] >= 1
    assert summary["after_isolated"] >= 1
    assert summary["by_state"].get("ORPHANED", 0) >= 1
    # metadata 计数落在 owner-home 副本库（run_legacy_migration 写副本）
    copy_repo = RuntimeRepository(home_dir / "runtime.db")
    meta = copy_repo.get_metadata("wake_legacy_migration:v1-test:counts")
    assert meta is not None
    import json as _json

    payload = _json.loads(meta)
    assert payload["after_isolated"] >= 1
    # 副本库台账已隔离 t2；t1（有 run）未隔离
    assert copy_repo.get_legacy_migration(t1) is None
    assert copy_repo.get_legacy_migration(t2) is not None
