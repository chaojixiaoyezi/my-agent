"""Owner runtime.db 权威实体测试（3.txt A 节主链 / D 节绑定 / F 节 pointer）。

覆盖：A.1 单库、A.3 runtime_events append-only、A.4 主链、A.5 root AgentRun、
A.6 attempt 先于委托、A.7 delegation、D.1 租户边界、D.5 root claims 唯一、
F.2/F.3 current pointer CAS、R1 legacy 投影改名迁移。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import (
    BINDING_ACTIVE,
    BINDING_SUPERSEDED,
    ROOT_CLAIM_CONFLICT,
    RuntimeConflictError,
    RuntimeRepository,
)
from agent_py_agent.agent.runtime_db.schema import RUNTIME_DB_FILENAME, runtime_db_path

# A.4 主链八表（tasks/task_runs/agent_runs/agent_attempts/delegations/
# workspace_bindings/root_claims/runtime_events + metadata）
_AUTHORITY_TABLES = {
    "metadata",
    "tasks",
    "task_runs",
    "agent_runs",
    "agent_attempts",
    "delegations",
    "workspace_bindings",
    "root_claims",
    "runtime_events",
}


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "runtime.db")


def _table_names(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


# ------------------------------------------------------------------ schema
def test_authority_tables_created(repo):
    assert _table_names(repo.db_path) >= _AUTHORITY_TABLES


def test_runtime_db_filename_is_single_authority_db(tmp_path):
    # A.1：每个 owner 只有一个权威 runtime.db，位于 owner home 根。
    assert runtime_db_path(tmp_path) == tmp_path / "runtime.db"
    assert RUNTIME_DB_FILENAME == "runtime.db"


def test_runtime_events_append_only_never_rewritten(repo):
    task = repo.create_task(owner_id="local/main")
    run = repo.create_agent_run(task_run_id=repo.create_task_run(task_id=task["task_id"])["task_run_id"])
    attempt = repo.create_attempt(run["agent_run_id"])
    first = repo.append_event(
        event_type="tool.open",
        attempt_id=attempt["attempt_id"],
        agent_run_id=run["agent_run_id"],
        task_run_id=run["task_run_id"],
        payload={"tool": "bash"},
    )
    second = repo.append_event(
        event_type="tool.close",
        attempt_id=attempt["attempt_id"],
        agent_run_id=run["agent_run_id"],
        payload={"tool": "bash"},
    )
    # A.8：事件必须追到 attempt；seq 单调；按追加顺序读出。
    assert first["seq"] < second["seq"]
    events = repo.events_for_attempt(attempt["attempt_id"])
    assert [item["event_type"] for item in events] == ["tool.open", "tool.close"]
    assert events[0]["payload"] == {"tool": "bash"}


# ------------------------------------------------------------ A.4/A.5 主链
def test_main_chain_task_taskrun_agentrun(repo):
    task = repo.create_task(owner_id="local/main", goal="做一个网站")
    task_run = repo.create_task_run(task_id=task["task_id"])
    root = repo.create_agent_run(task_run_id=task_run["task_run_id"])
    # A.5：每个 TaskRun 建一个 root AgentRun；A.7：root 的 parent 为空是合法根身份。
    assert root["parent_agent_run_id"] == ""
    assert root["delegation_id"] == ""
    assert repo.get_task_run(root["task_run_id"])["task_run_id"] == task_run["task_run_id"]
    assert repo.agent_runs_for_task_run(task_run["task_run_id"])[0]["agent_run_id"] == root["agent_run_id"]


def test_framework_ids_shape(repo):
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    root = repo.create_agent_run(task_run_id=task_run["task_run_id"])
    attempt = repo.create_attempt(root["agent_run_id"])
    assert task["task_id"].startswith("task-")
    assert task_run["task_run_id"].startswith("taskrun-")
    assert root["agent_run_id"].startswith("agentrun-")
    assert attempt["attempt_id"].startswith("attempt-")


# -------------------------------------------------------- F.2/F.3 CAS pointer
def test_attempt_cas_generations_and_current_pointer(repo):
    task = repo.create_task(owner_id="local/main")
    run = repo.create_agent_run(
        task_run_id=repo.create_task_run(task_id=task["task_id"])["task_run_id"]
    )
    first = repo.create_attempt(run["agent_run_id"])
    second = repo.create_attempt(run["agent_run_id"])
    third = repo.create_attempt(run["agent_run_id"])
    assert (first["attempt_generation"], second["attempt_generation"], third["attempt_generation"]) == (1, 2, 3)
    # F.2：current pointer 指向最新 attempt。
    current = repo.current_attempt(run["agent_run_id"])
    assert current["attempt_id"] == third["attempt_id"]
    assert current["attempt_generation"] == 3
    # F.7：旧 attempt 行是不可变历史，全部保留。
    gens = [item["attempt_generation"] for item in repo.attempts_for_run(run["agent_run_id"])]
    assert gens == [1, 2, 3]


def test_attempt_cas_continues_from_external_mutation(repo):
    # CAS 语义：从当前 pointer 读到的 generation 递增，不依赖调用方旧值。
    task = repo.create_task(owner_id="local/main")
    run = repo.create_agent_run(
        task_run_id=repo.create_task_run(task_id=task["task_id"])["task_run_id"]
    )
    repo.create_attempt(run["agent_run_id"])
    with repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET current_attempt_generation = 5 WHERE agent_run_id = ?",
            (run["agent_run_id"],),
        )
        conn.commit()
    attempt = repo.create_attempt(run["agent_run_id"])
    assert attempt["attempt_generation"] == 6


def test_attempt_on_missing_run_rejected(repo):
    with pytest.raises(KeyError, match="不存在"):
        repo.create_attempt("agentrun-0000-00000000")


# ------------------------------------------------------------ A.6/A.7 委托
def test_delegation_requires_child_attempt(repo):
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    parent = repo.create_agent_run(task_run_id=task_run["task_run_id"])
    child = repo.create_agent_run(task_run_id=task_run["task_run_id"], parent_agent_run_id=parent["agent_run_id"])
    # A.6：child 必须先建自己的 attempt 才能执行工具/被委托。
    with pytest.raises(RuntimeConflictError, match="不是其 current pointer"):
        repo.create_delegation(
            parent_agent_run_id=parent["agent_run_id"],
            child_agent_run_id=child["agent_run_id"],
            child_attempt_id="attempt-0000-00000000",
        )
    child_attempt = repo.create_attempt(child["agent_run_id"])
    delegation = repo.create_delegation(
        parent_agent_run_id=parent["agent_run_id"],
        child_agent_run_id=child["agent_run_id"],
        child_attempt_id=child_attempt["attempt_id"],
        granted_scope={"allow": ["bash", "read"]},
    )
    assert delegation["delegation_id"].startswith("delegation-")
    assert delegation["child_attempt_id"] == child_attempt["attempt_id"]
    # child 的 delegation_id 回填（A.7：child 经 immutable Delegation 连 parent）。
    assert repo.get_agent_run(child["agent_run_id"])["delegation_id"] == delegation["delegation_id"]
    assert repo.delegation_for_child(child["agent_run_id"])["parent_agent_run_id"] == parent["agent_run_id"]
    # immutable：同一 parent/child 对不能重复委托。
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_delegation(
            parent_agent_run_id=parent["agent_run_id"],
            child_agent_run_id=child["agent_run_id"],
            child_attempt_id=child_attempt["attempt_id"],
        )


def test_root_run_has_no_delegation(repo):
    # A.7：root 的 parent/delegation 为空是合法根身份。
    task = repo.create_task(owner_id="local/main")
    root = repo.create_agent_run(
        task_run_id=repo.create_task_run(task_id=task["task_id"])["task_run_id"]
    )
    assert repo.delegation_for_child(root["agent_run_id"]) is None


# ------------------------------------------------- D 节 binding / root claims
def test_binding_roundtrip_and_active_selection(repo):
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    run = repo.create_agent_run(task_run_id=task_run["task_run_id"])
    attempt = repo.create_attempt(run["agent_run_id"])
    binding = repo.create_binding(
        agent_run_id=run["agent_run_id"],
        attempt_id=attempt["attempt_id"],
        owner_id="local/main",
        root_path=str(task_run["task_run_id"]),
        readable_roots=["/home/root"],
        writable_roots=["/home/root/work"],
        extra_write_roots=["/home/root/output"],
        roots_digest="sha256:abc",
    )
    assert binding["status"] == BINDING_ACTIVE
    assert repo.binding_for_run(run["agent_run_id"])["binding_id"] == binding["binding_id"]


def test_binding_supersede_cas_epoch(repo):
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    run = repo.create_agent_run(task_run_id=task_run["task_run_id"])
    attempt = repo.create_attempt(run["agent_run_id"])
    binding = repo.create_binding(
        agent_run_id=run["agent_run_id"],
        attempt_id=attempt["attempt_id"],
        owner_id="local/main",
        root_path="/root",
    )
    assert binding["workspace_epoch"] == 1
    # D.9：epoch 不匹配 → CAS 失败。
    with pytest.raises(RuntimeConflictError, match="binding 迁移 CAS 失败"):
        repo.supersede_binding(binding_id=binding["binding_id"], expected_epoch=9)
    new_epoch = repo.supersede_binding(binding_id=binding["binding_id"], expected_epoch=1)
    assert new_epoch == 2
    # F.1：epoch 只在 binding 迁移时变化；旧 ACTIVE 已 SUPERSEDED。
    old = repo.get_binding(binding["binding_id"])
    assert old["status"] == BINDING_SUPERSEDED
    assert old["workspace_epoch"] == 2
    assert repo.binding_for_run(run["agent_run_id"]) is None


def test_root_claim_unique_per_physical_root(repo):
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    run = repo.create_agent_run(task_run_id=task_run["task_run_id"])
    attempt = repo.create_attempt(run["agent_run_id"])
    binding = repo.create_binding(
        agent_run_id=run["agent_run_id"],
        attempt_id=attempt["attempt_id"],
        owner_id="local/main",
        root_path="/home/u/work",
    )
    claim = repo.claim_root(
        binding_id=binding["binding_id"],
        agent_run_id=run["agent_run_id"],
        attempt_id=attempt["attempt_id"],
        root_path="/home/u/work",
    )
    assert repo.claim_for_root("/home/u/work")["claim_id"] == claim["claim_id"]
    # D.5：同一物理写根在库内唯一声明，二次声明拒绝。
    with pytest.raises(RuntimeConflictError, match=ROOT_CLAIM_CONFLICT):
        repo.claim_root(
            binding_id=binding["binding_id"],
            agent_run_id=run["agent_run_id"],
            attempt_id=attempt["attempt_id"],
            root_path="/home/u/work",
        )


# ------------------------------------------------------------- A.9 事务原子
def test_transaction_rolls_back_all_entities(repo):
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    with pytest.raises(RuntimeError, match="boom"):
        with repo.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id,
                                       delegation_id, run_id, role, status,
                                       current_attempt_id, current_attempt_generation,
                                       workspace_epoch, created_at, updated_at)
                VALUES('agentrun-0000-00000000', ?, '', '', '', '', '', '', 0, 1, 1, 1)
                """,
                (task_run["task_run_id"],),
            )
            conn.execute(
                "INSERT INTO task_runs(task_run_id, task_id, created_at, updated_at) VALUES('taskrun-x', ?, 1, 1)",
                (task["task_id"],),
            )
            raise RuntimeError("boom")
    assert repo.get_agent_run("agentrun-0000-00000000") is None
    assert repo.get_task_run("taskrun-x") is None


# ---------------------------------------------- R1 legacy 投影改名迁移
def test_legacy_agent_runs_rename_preserves_data(tmp_path):
    # 模拟 R1 前安装：投影库里有旧 agent_runs 表和数据。
    db_path = tmp_path / "old_local.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY, root_task_id TEXT NOT NULL, parent_run_id TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT '', agent_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '', progress REAL NOT NULL DEFAULT 0, current_step TEXT NOT NULL DEFAULT '',
            latest_summary TEXT NOT NULL DEFAULT '', workspace_path TEXT NOT NULL DEFAULT '',
            checkpoint_ref TEXT NOT NULL DEFAULT '', latest_compact_ref TEXT NOT NULL DEFAULT '',
            compact_count INTEGER NOT NULL DEFAULT 0, heartbeat_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_runs(run_id, root_task_id, status, created_at, updated_at) VALUES('subagent-111-aaa', 'root', 'DONE', 1, 2)"
    )
    conn.commit()
    conn.close()
    # LocalStore 初始化触发 R1 迁移。
    LocalStore(db_path, enable_fts=False)
    tables = _table_names(db_path)
    # 新旧不得同名双权威：旧名必须消失，数据保留在新名下。
    assert "agent_runs" not in tables
    assert "legacy_agent_runs" in tables
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT run_id, status FROM legacy_agent_runs WHERE run_id = 'subagent-111-aaa'"
        ).fetchone()
        assert tuple(row) == ("subagent-111-aaa", "DONE")
    finally:
        conn.close()


def test_fresh_local_store_never_creates_agent_runs(tmp_path):
    # 新库直接建 legacy_agent_runs，不存在与权威库同名的 agent_runs。
    store = LocalStore(tmp_path / "new_local.db", enable_fts=False)
    tables = _table_names(store.db_path)
    assert "agent_runs" not in tables
    assert "legacy_agent_runs" in tables


def test_legacy_upsert_read_after_rename(tmp_path):
    # 改名后投影写读链路仍可用（upsert 走 legacy_agent_runs）。
    store = LocalStore(tmp_path / "cp.db", enable_fts=False)
    from agent_py_agent.agent.local_storage.control_plane_models import AgentRunRecord

    record = AgentRunRecord(run_id="subagent-222-bbb", root_task_id="root", status="RUNNING")
    stored = store.upsert_agent_run(record)
    assert stored.run_id == "subagent-222-bbb"
    fetched = store.get_agent_run("subagent-222-bbb")
    assert fetched is not None and fetched.status == "RUNNING"
