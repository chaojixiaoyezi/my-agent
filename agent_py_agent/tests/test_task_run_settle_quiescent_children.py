"""子代理静止但未终态时，父 TaskRun 仍要能收口（2026-09-28）。

背景：子代理以 BLOCKED / unfinished 这类非终态 runtime_status 结束时，设计上只关闭 attempt、删掉执行锁，
agent_runs.status 停在 created（展示面读 canonical，不受影响）。settle_task_run_if_agent_tree_terminal 原来要求
同一 TaskRun 下所有 AgentRun 都终态，父 TaskRun 因此永远 closed_at=0、不写 task_run.closed；两个调用方都忽略返回值，
open_task_runs 只增不减，发现扫描每一轮都对这些行白跑一次。
锁定：
- 非根 run 满足“状态终态”或“current attempt 已结算（done/failed/cancelled/recovered）且没有执行锁”之一即视为静止；
  unknown（结果不明）即使锁已不在也不算，要等显式恢复；根 run 仍须终态（TaskRun 状态取根）。
- task_run.closed 的 payload 带静止但未终态的子 run 计数与 ID，作为结构化证据。
- 只用 runtime 自己的事实；子 run 之后 create_attempt 经 _reopen_task_run_for_attempt_conn 写 task_run.reopened，可逆。
- attempt 正在跑或仍持有执行锁的子 run：仍 agent_tree_active。
- 发现扫描（unfinished_task_ids）能把存量 open TaskRun 关掉，且幂等。
"""

from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids
from agent_py_agent.agent.runtime_db.operations import exec_lock_scope
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _tree(repo, *, task_id: str = "") -> dict[str, str]:
    """根 run 已 done、一个子 run 仍在跑：按真实派工路径登记 pending 再激活，attempt running 且持执行锁。"""
    root = repo.record_run_creation(
        owner_id="local/main", goal="parent", conversation_task_id=task_id, run_id="run-1", role="main",
    )
    child = repo.record_run_creation(
        owner_id="local/main", goal="child", run_id="child-1", role="worker", parent_run_id="run-1",
        attempt_status="pending",
    )
    assert child["task_run_id"] == root["task_run_id"]
    activated = repo.create_attempt(child["agent_run_id"], reuse_pending=True)
    assert activated["attempt_id"] == child["attempt_id"] and _lock_rows(repo, child["agent_run_id"])
    repo.settle_agent_run(agent_run_id=root["agent_run_id"], status="done", attempt_id=root["attempt_id"])
    return {"root": root, "child": child, "task_run_id": root["task_run_id"], "task_id": root["task_id"]}


def _block_child(repo, tree) -> None:
    """按运行时真实收口路径让子 run 以 BLOCKED 结束：attempt 关闭、锁删除、run 仍 created。"""
    child = tree["child"]
    assert _lock_rows(repo, child["agent_run_id"])
    settled = repo.settle_agent_attempt(
        agent_run_id=child["agent_run_id"], attempt_id=child["attempt_id"],
        payload={"status": "attempt_done", "runtime_status": "blocked", "runtime_reason": "TOOL_PROTOCOL_VIOLATION"},
    )
    assert settled["settled"] is True
    assert _run_status(repo, child["agent_run_id"]) == "created"
    assert not _lock_rows(repo, child["agent_run_id"])


def _run_status(repo, agent_run_id: str) -> str:
    row = repo._runtime_connect().execute(
        "SELECT status FROM agent_runs WHERE agent_run_id = ?", (agent_run_id,),
    ).fetchone()
    return str(row["status"] or "")


def _lock_rows(repo, agent_run_id: str) -> list:
    return repo._runtime_connect().execute(
        "SELECT * FROM resource_locks WHERE canonical_scope = ?", (exec_lock_scope(agent_run_id),),
    ).fetchall()


def _task_run(repo, task_run_id: str):
    return repo._runtime_connect().execute(
        "SELECT * FROM task_runs WHERE task_run_id = ?", (task_run_id,),
    ).fetchone()


def _events(repo, task_run_id: str, event_type: str) -> list[dict]:
    rows = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE task_run_id = ? AND event_type = ? ORDER BY seq",
        (task_run_id, event_type),
    ).fetchall()
    return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]


def _sql(repo, statement: str, params: tuple) -> None:
    conn = repo._runtime_connect()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def test_blocked_child_lets_terminal_root_close_task_run(repo):
    tree = _tree(repo)
    _block_child(repo, tree)
    assert [row["task_run_id"] for row in repo.open_task_runs()] == [tree["task_run_id"]]

    result = repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=tree["task_run_id"], task_id=tree["task_id"], operator="conversation-runtime",
        reason="conversation_task_completed",
    )

    assert result["settled"] is True and result["status"] == "done"
    task_run = _task_run(repo, tree["task_run_id"])
    assert task_run["status"] == "done" and task_run["closed_at"] > 0
    assert repo.open_task_runs() == []
    closed, = _events(repo, tree["task_run_id"], "task_run.closed")
    payload = closed["payload"]
    assert payload["status"] == "done" and payload["root_agent_run_id"] == tree["root"]["agent_run_id"]
    assert payload["agent_run_count"] == 2
    # 结构化证据：哪些子 run 是“静止但未终态”地被算作已结束的。
    assert payload["quiescent_agent_run_count"] == 1
    assert payload["quiescent_agent_run_ids"] == [tree["child"]["agent_run_id"]]
    # 子 run 本身不被改写：仍是 created，attempt 已关闭，无锁。
    assert _run_status(repo, tree["child"]["agent_run_id"]) == "created"


def test_quiescent_child_new_attempt_reopens_task_run_and_can_close_again(repo):
    tree = _tree(repo)
    _block_child(repo, tree)
    assert repo.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"])["settled"] is True
    closed_at = _task_run(repo, tree["task_run_id"])["closed_at"]

    resumed = repo.create_attempt(tree["child"]["agent_run_id"])

    current = _task_run(repo, tree["task_run_id"])
    assert current["closed_at"] == 0 and current["status"] == "created"
    reopened, = _events(repo, tree["task_run_id"], "task_run.reopened")
    assert reopened["attempt_id"] == resumed["attempt_id"]
    assert reopened["agent_run_id"] == tree["child"]["agent_run_id"]
    assert reopened["payload"] == {"previous_status": "done", "previous_closed_at": closed_at}
    # 子 run 重新在跑（attempt running、持锁）：TaskRun 不能再关。
    assert repo.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"]) == {
        "settled": False, "reason": "agent_tree_active",
    }
    repo.settle_agent_run(agent_run_id=tree["child"]["agent_run_id"], status="failed", attempt_id=resumed["attempt_id"])
    again = repo.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"])
    assert again["settled"] is True and again["status"] == "done"
    first, second = _events(repo, tree["task_run_id"], "task_run.closed")
    assert first["payload"]["quiescent_agent_run_count"] == 1
    # 这次子 run 已经真终态，没有“静止但未终态”的成员。
    assert second["payload"]["quiescent_agent_run_count"] == 0
    assert second["payload"]["quiescent_agent_run_ids"] == []


@pytest.mark.parametrize("shape", ["attempt_running", "terminal_attempt_still_locked", "root_not_terminal"])
def test_running_or_locked_child_and_active_root_keep_task_run_open(repo, shape):
    tree = _tree(repo)
    child = tree["child"]
    if shape == "terminal_attempt_still_locked":
        # attempt 已结算（recovered）但执行锁还在：锁是“仍有执行者”的运行时事实，不算静止。
        _sql(repo, "UPDATE agent_attempts SET status = 'recovered', ended_at = 1 WHERE attempt_id = ?",
             (child["attempt_id"],))
        assert _lock_rows(repo, child["agent_run_id"])
    elif shape == "root_not_terminal":
        # 根未终态、子已终态：TaskRun 状态取根，根必须终态。
        _sql(repo, "UPDATE agent_runs SET status = 'created' WHERE agent_run_id = ?", (tree["root"]["agent_run_id"],))
        repo.settle_agent_run(agent_run_id=child["agent_run_id"], status="done", attempt_id=child["attempt_id"])

    result = repo.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"], task_id=tree["task_id"])

    assert result == {"settled": False, "reason": "agent_tree_active"}
    assert _task_run(repo, tree["task_run_id"])["closed_at"] == 0
    assert _events(repo, tree["task_run_id"], "task_run.closed") == []


def test_unknown_attempt_without_lock_keeps_task_run_open(repo):
    """unknown 是“结果不明”：即使执行锁已不在也不算静止，要等显式恢复或结算，不能让 task_run.closed 盖过没有定论的子结果。"""
    tree = _tree(repo)
    child = tree["child"]
    _sql(repo, "UPDATE agent_attempts SET status = 'unknown', ended_at = 1 WHERE attempt_id = ?", (child["attempt_id"],))
    assert _lock_rows(repo, child["agent_run_id"])
    _sql(repo, "DELETE FROM resource_locks WHERE canonical_scope = ?", (exec_lock_scope(child["agent_run_id"]),))

    result = repo.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"])

    assert result == {"settled": False, "reason": "agent_tree_active"}
    assert _task_run(repo, tree["task_run_id"])["closed_at"] == 0
    assert _events(repo, tree["task_run_id"], "task_run.closed") == []
    assert repo.get_attempt(child["attempt_id"])["status"] == "unknown"


def test_recovered_attempt_without_lock_counts_as_quiescent_child(repo):
    """recovered 表示恢复协议已经结算过：锁已不在就算静止，TaskRun 可关（仍可逆）。"""
    tree = _tree(repo)
    child = tree["child"]
    _sql(repo, "UPDATE agent_attempts SET status = 'recovered', ended_at = 1 WHERE attempt_id = ?", (child["attempt_id"],))
    _sql(repo, "DELETE FROM resource_locks WHERE canonical_scope = ?", (exec_lock_scope(child["agent_run_id"]),))

    result = repo.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"])

    assert result["settled"] is True
    closed, = _events(repo, tree["task_run_id"], "task_run.closed")
    assert closed["payload"]["quiescent_agent_run_ids"] == [child["agent_run_id"]]
    assert repo.get_attempt(child["attempt_id"])["status"] == "recovered"


def test_child_without_any_attempt_keeps_task_run_open(repo):
    tree = _tree(repo)
    orphan = repo.create_agent_run(task_run_id=tree["task_run_id"], parent_agent_run_id=tree["root"]["agent_run_id"])
    _block_child(repo, tree)

    result = repo.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"])

    # 没有任何 attempt 的子 run 既不是终态也证明不了已静止：保持开放。
    assert result == {"settled": False, "reason": "agent_tree_active"}
    assert str(orphan["current_attempt_id"] or "") == ""


def test_discovery_scan_closes_open_task_run_with_quiescent_child(tmp_path):
    home = tmp_path / "home"
    repo = RuntimeRepository(home / "runtime.db")
    task_id = "goal-with-blocked-child"
    tree = _tree(repo, task_id=task_id)
    _block_child(repo, tree)
    link_dir = home / "workspace" / "runtime" / "workspaces" / "goal" / "conversations" / "tasks"
    link_dir.mkdir(parents=True)
    (link_dir / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id, "status": "completed", "thread_id": "thread-goal", "goal": "parent",
    }), encoding="utf-8")
    assert [row["task_run_id"] for row in repo.open_task_runs()] == [tree["task_run_id"]]

    assert unfinished_task_ids(home) == []
    assert unfinished_task_ids(home) == []

    assert repo.open_task_runs() == []
    task_run = _task_run(repo, tree["task_run_id"])
    assert task_run["status"] == "done" and task_run["closed_at"] > 0
    closed, = _events(repo, tree["task_run_id"], "task_run.closed")
    assert closed["payload"]["operator"] == "wake-discovery-task-run-reconcile"
    assert closed["payload"]["quiescent_agent_run_ids"] == [tree["child"]["agent_run_id"]]


def test_pending_activation_started_events_report_their_own_columns(repo):
    """agent_run.started 记 agent_runs 列（created），agent_attempt.started 记 attempt 列（running），不再混写。"""
    root = repo.record_run_creation(owner_id="local/main", goal="parent", run_id="run-1", role="main")
    child = repo.record_run_creation(
        owner_id="local/main", goal="child", run_id="child-1", role="worker", parent_run_id="run-1",
        attempt_status="pending",
    )
    activated = repo.create_attempt(child["agent_run_id"], reuse_pending=True)
    assert activated["attempt_id"] == child["attempt_id"] and activated["status"] == "running"
    events = {row["event_type"]: json.loads(row["payload_json"]) for row in repo._runtime_connect().execute(
        "SELECT event_type, payload_json FROM runtime_events WHERE attempt_id = ? "
        "AND event_type IN ('agent_attempt.started', 'agent_run.started')",
        (child["attempt_id"],),
    ).fetchall()}
    assert events["agent_attempt.started"] == {"previous_status": "pending", "status": "running", "attempt_generation": 1}
    assert events["agent_run.started"]["status"] == _run_status(repo, child["agent_run_id"]) == "created"
    assert events["agent_run.started"]["attempt_generation"] == 1
    assert root["task_run_id"] == child["task_run_id"]
