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

from agent_py_agent.agent.owner_wake_discovery import (
    _has_unfinished_subagent_run,
    discover_wake_pending_owners,
    unfinished_task_ids,
)
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


# LLM: 使用真实RuntimeRepository收口路径，投影只表达BLOCKED，不把它改为终态来掩盖发现缺陷。
# 函数用途: 准备一个可恢复但已静止的群owner及其权威运行树。
def _b4_quiet_owner(tmp_path, *, closed=True, history=0, data_layout=False):
    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "groups" / "quiet-group"
    runtime = RuntimeRepository(home / ("data/runtime.db" if data_layout else "runtime.db"))
    tree = _tree(runtime, task_id="b4-recoverable-task")
    _block_child(runtime, tree)
    if closed:
        assert runtime.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"])["settled"]
    path = home / "agents" / "child-1" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "owner-agent-projection.v1", "run_id": "child-1",
                                "root_task_id": tree["task_id"], "status": "BLOCKED"}), encoding="utf-8")
    for index in range(history):
        old = home / "agents" / f"old-{index:03d}" / "state.json"
        old.parent.mkdir(parents=True)
        old.write_text('{"status":"DONE"}', encoding="utf-8")
    return owners, home, runtime, tree, path


# LLM: 比较业务表的完整行而非只比事件数，发现探针不能顺手取消、结算、改attempt或删锁。
# 函数用途: 取得运行树及事件的只读快照用于检查无写副作用。
def _b4_runtime_rows(runtime):
    with runtime._runtime_connection() as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("task_runs", "agent_runs", "agent_attempts", "resource_locks", "runtime_events")}


# LLM: 冷扫描及热缓存都不得把静止BLOCKED当硬唤醒，且必须保留created/BLOCKED以供新attempt恢复。
# 函数用途: 验证已关闭任务的静止子代理停止周期空转且不改权威状态。
@pytest.mark.parametrize("history", [0, 40])
def test_b4_settled_blocked_child_does_not_wake_or_mutate(tmp_path, history):
    owners, home, runtime, tree, path = _b4_quiet_owner(tmp_path, history=history)
    before, state_bytes = _b4_runtime_rows(runtime), path.read_bytes()
    for _ in range(2):
        assert _has_unfinished_subagent_run(home) is False
        assert discover_wake_pending_owners(owners) == []
    assert _b4_runtime_rows(runtime) == before
    assert _run_status(runtime, tree["child"]["agent_run_id"]) == "created"
    assert path.read_bytes() == state_bytes and json.loads(state_bytes)["status"] == "BLOCKED"


# LLM: 未补closed_at但已满足原树静止判据时可免轮询；探针必须只读，不能调用settle入口。
# 函数用途: 验证可结算的静止树不会因总账尚未补关而持续空唤醒。
def test_b4_settleable_tree_is_read_only_and_not_an_unfinished_subagent(tmp_path):
    _owners, home, runtime, _tree_row, path = _b4_quiet_owner(tmp_path, closed=False)
    before, state_bytes = _b4_runtime_rows(runtime), path.read_bytes()
    assert _has_unfinished_subagent_run(home) is False
    assert _b4_runtime_rows(runtime) == before and path.read_bytes() == state_bytes


# LLM: 原owner投影的task_id来自SubAgentTask.root_id，即根run_id，不保证等于runtime内部Task主键。
# 函数用途: 验证按真实投影身份仍能定位同一父TaskRun，而非因两类ID不同继续空唤醒。
@pytest.mark.parametrize("field", ["task_id", "root_task_id"])
def test_b4_projection_root_run_id_resolves_its_own_parent_task(tmp_path, field):
    owners, home, runtime, tree, path = _b4_quiet_owner(tmp_path)
    root = runtime.get_agent_run(tree["root"]["agent_run_id"])
    assert root["run_id"] != tree["task_id"]
    payload = json.loads(path.read_text())
    payload.pop("root_task_id")
    payload[field] = root["run_id"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    before, state_bytes = _b4_runtime_rows(runtime), path.read_bytes()
    assert _has_unfinished_subagent_run(home) is False
    assert discover_wake_pending_owners(owners) == []
    assert _b4_runtime_rows(runtime) == before and path.read_bytes() == state_bytes


# LLM: closed TaskRun不能盖过新活跃/未知attempt、残留执行锁、活跃根或收口重试事实。
# 函数用途: 验证每一种不可证明静止的运行事实仍保留唤醒。
@pytest.mark.parametrize("fact", ["closeout", "running", "starting", "pending", "unknown", "historical_unknown",
                                  "locked", "parent_live", "run_running", "run_starting", "missing_attempt",
                                  "missing_agent", "wrong_task", "planning", "pending_projection", "running_projection"])
def test_b4_blocked_child_with_live_or_uncertain_fact_still_wakes(tmp_path, fact):
    owners, home, runtime, tree, path = _b4_quiet_owner(tmp_path)
    child = tree["child"]
    payload = json.loads(path.read_text())
    if fact == "closeout":
        payload["runtime_closeout_pending"] = True
    elif fact in {"running", "starting", "pending", "unknown"}:
        _sql(runtime, "UPDATE agent_attempts SET status = ? WHERE attempt_id = ?", (fact, child["attempt_id"]))
    elif fact in {"locked", "historical_unknown"}:
        resumed = runtime.create_attempt(child["agent_run_id"])
        if fact == "locked":
            _sql(runtime, "UPDATE agent_attempts SET status = 'done', ended_at = 1 WHERE attempt_id = ?", (resumed["attempt_id"],))
            runtime.settle_task_run_terminal(task_run_id=tree["task_run_id"], status="done")
        else:
            runtime.settle_agent_attempt(agent_run_id=child["agent_run_id"], attempt_id=resumed["attempt_id"],
                                         payload={"runtime_status": "blocked"})
            runtime.settle_task_run_if_agent_tree_terminal(task_run_id=tree["task_run_id"])
            _sql(runtime, "UPDATE agent_attempts SET status = 'unknown' WHERE attempt_id = ?", (child["attempt_id"],))
    elif fact == "parent_live":
        _sql(runtime, "UPDATE agent_runs SET status = 'created' WHERE agent_run_id = ?", (tree["root"]["agent_run_id"],))
        _sql(runtime, "UPDATE task_runs SET status = 'created', closed_at = 0 WHERE task_run_id = ?", (tree["task_run_id"],))
    elif fact.startswith("run_"):
        _sql(runtime, "UPDATE agent_runs SET status = ? WHERE agent_run_id = ?", (fact[4:], child["agent_run_id"]))
    elif fact == "missing_attempt":
        _sql(runtime, "UPDATE agent_runs SET current_attempt_id = 'missing' WHERE agent_run_id = ?", (child["agent_run_id"],))
    elif fact == "missing_agent":
        payload["run_id"] = "missing"
    elif fact == "wrong_task":
        payload["root_task_id"] = "another-task"
    else:
        payload["status"] = {"planning": "PLANNING", "pending_projection": "PENDING", "running_projection": "RUNNING"}[fact]
    path.write_text(json.dumps(payload), encoding="utf-8")
    before, state_bytes = _b4_runtime_rows(runtime), path.read_bytes()
    assert _has_unfinished_subagent_run(home) is True
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["quiet-group"]
    assert _b4_runtime_rows(runtime) == before and path.read_bytes() == state_bytes


# LLM: run终态不是坏current指针的免责条件；悬空或跨run的attempt都不能作为静止证明。
# 函数用途: 复现独立评审指出的终态run错误身份被静止短路的问题。
@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
@pytest.mark.parametrize("pointer", ["missing", "foreign"])
def test_b4_terminal_run_with_invalid_current_attempt_keeps_waking(tmp_path, status, pointer):
    owners, home, runtime, tree, path = _b4_quiet_owner(tmp_path)
    attempt_id = "missing-attempt"
    if pointer == "foreign":
        other = runtime.record_run_creation(owner_id="local/main", goal="another tree", run_id="another-root", role="main")
        attempt_id = other["attempt_id"]
        assert runtime.get_attempt(attempt_id)["status"] == "running"
    _sql(runtime, "UPDATE agent_runs SET status = ?, current_attempt_id = ? WHERE agent_run_id = ?",
         (status, attempt_id, tree["child"]["agent_run_id"]))
    before, state_bytes = _b4_runtime_rows(runtime), path.read_bytes()
    assert _has_unfinished_subagent_run(home) is True
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["quiet-group"]
    assert _b4_runtime_rows(runtime) == before and path.read_bytes() == state_bytes


# LLM: 合法终态run不强制存在current attempt；没有指针与指针损坏是两种不同事实。
# 函数用途: 验证修复坏指针时不误伤无current指针的已关闭子run。
def test_b4_terminal_run_without_current_pointer_remains_quiet(tmp_path):
    owners, home, runtime, tree, path = _b4_quiet_owner(tmp_path)
    _sql(runtime, "UPDATE agent_runs SET status = 'done', current_attempt_id = '' WHERE agent_run_id = ?",
         (tree["child"]["agent_run_id"],))
    before, state_bytes = _b4_runtime_rows(runtime), path.read_bytes()
    assert _has_unfinished_subagent_run(home) is False
    assert discover_wake_pending_owners(owners) == []
    assert _b4_runtime_rows(runtime) == before and path.read_bytes() == state_bytes


# LLM: 无权威库、无记录或查询失败不能当作已静止，closeout_pending则更不能被库读取遮蔽。
# 函数用途: 验证缺失或损坏运行权威时发现层保持原有唤醒行为。
@pytest.mark.parametrize("authority", ["missing_db", "unavailable", "error"])
def test_b4_unavailable_runtime_authority_keeps_blocked_child_visible(tmp_path, monkeypatch, authority):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    path = home / "agents" / "child-1" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"run_id":"child-1","status":"BLOCKED"}', encoding="utf-8")
    if authority == "unavailable":
        monkeypatch.setattr(discovery, "_runtime_repo_for_owner", lambda _home: None)
    elif authority == "error":
        # LLM: 只替换库入口，模拟读取失败而不改任何运行状态。
        # 函数用途: 注入运行权威不可读错误。
        def unavailable(_home):
            raise OSError("fixture runtime authority unavailable")
        monkeypatch.setattr(discovery, "_runtime_repo_for_owner", unavailable)
    assert _has_unfinished_subagent_run(home) is True
    assert json.loads(path.read_text())["status"] == "BLOCKED"
    assert not (home / "runtime.db").exists()


# LLM: 新attempt通过原协议重开TaskRun；即使state.json字节不变且主库mtime不变，WAL也应立即打破静止缓存。
# 函数用途: 验证静止owner恢复执行后能马上被直接探针和完整发现入口重新找到。
def test_b4_new_attempt_reopens_task_and_invalidates_quiet_owner_cache(tmp_path, monkeypatch):
    from contextlib import closing

    from agent_py_agent.agent import owner_wake_discovery as discovery

    owners, home, runtime, tree, path = _b4_quiet_owner(tmp_path)
    monkeypatch.setattr(discovery, "_FACT_MIN_CACHED_SECONDS", 0.0)
    state_bytes = path.read_bytes()
    # 领域借用现在包着写代际栅栏，WAL checkpoint 会先被 BEGIN IMMEDIATE 包进事务；锚连接改用原物理连接。
    with closing(runtime._runtime_connect()) as anchor:
        anchor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert discover_wake_pending_owners(owners) == []
        assert discover_wake_pending_owners(owners) == []
        assert discovery._OWNER_FACT_CACHE[str(home)].kind == "none"
        stamp = (home / "runtime.db").stat().st_mtime_ns
        resumed = runtime.create_attempt(tree["child"]["agent_run_id"])
        assert (home / "runtime.db").stat().st_mtime_ns == stamp
        assert runtime.get_attempt(resumed["attempt_id"])["status"] == "running"
        assert _task_run(runtime, tree["task_run_id"])["closed_at"] == 0
        assert _has_unfinished_subagent_run(home) is True
        assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["quiet-group"]
    assert path.read_bytes() == state_bytes


# LLM: 静止只移除周期运行源，不能屏蔽用户/审批回调写入的既有wake_queue硬事实。
# 函数用途: 验证静止owner收到外部恢复队列信号时立即被完整发现入口唤醒。
@pytest.mark.parametrize("queue", ["urgent", "normal"])
def test_b4_quiet_owner_is_woken_by_external_wake_signal(tmp_path, monkeypatch, queue):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    owners, home, _runtime, _tree_row, path = _b4_quiet_owner(tmp_path)
    monkeypatch.setattr(discovery, "_FACT_MIN_CACHED_SECONDS", 0.0)
    assert discover_wake_pending_owners(owners) == []
    state_bytes = path.read_bytes()
    wake = home / "conversations" / "wake_queue" / queue / "resume.json"
    wake.parent.mkdir(parents=True)
    wake.write_text('{"status":"pending"}', encoding="utf-8")
    assert _has_unfinished_subagent_run(home) is False
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["quiet-group"]
    assert path.read_bytes() == state_bytes


# LLM: 同时覆盖任务指定data布局与仓库现行根目录布局，防只测未被真实探针打开的路径而掩盖冷缓存初始化。
# 函数用途: 验证旧库冷读不迁移、不切journal、不新建sidecar，无法证明静止时仍唤醒。
@pytest.mark.parametrize("layout", ["data", "root"])
def test_b4_cold_probe_does_not_initialize_or_migrate_legacy_db(tmp_path, layout):
    import sqlite3
    from contextlib import closing

    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    state = home / "agents" / "legacy-child" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"run_id":"legacy-child","status":"BLOCKED"}', encoding="utf-8")
    db = home / "data/runtime.db" if layout == "data" else home / "runtime.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        conn.execute("CREATE TABLE legacy_probe(value TEXT)")
        conn.execute("INSERT INTO legacy_probe VALUES ('untouched')")
        conn.commit()
    discovery._RUNTIME_REPO_CACHE.clear()
    before = db.read_bytes(), db.stat().st_mtime_ns, sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert _has_unfinished_subagent_run(home) is True
    after = db.read_bytes(), db.stat().st_mtime_ns, sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert after == before
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not discovery._RUNTIME_REPO_CACHE


# LLM: 同时测字母顺序与倒序扫描；静止项必须continue，不得提前把owner整体判静止。
#   分页器按 os.scandir 的目录原序扫描（ext4 哈希序随文件系统而变），这里只把 agents 根的列举固定成名字倒序，
#   让 z_ 静止项确定先于活跃兄弟被读到；判据和分页逻辑仍走原实现。
# 函数用途: 验证一个静止子代理不屏蔽同owner下另一棵树的活跃子代理。
@pytest.mark.parametrize("quiet_name,active_name", [("a_quiescent", "z_active"), ("z_quiescent", "a_active")])
def test_b4_quiescent_child_before_active_sibling_still_discovers_owner(tmp_path, monkeypatch, quiet_name, active_name):
    import os
    from pathlib import Path

    from agent_py_agent.agent import owner_wake_discovery as discovery
    from agent_py_agent.agent.subagents.services.persistence import locator_hygiene

    owners, home, runtime, _tree_row, path = _b4_quiet_owner(tmp_path)
    real_scandir, agents_root = locator_hygiene.os.scandir, home / "agents"

    # LLM: 只给 agents 根返回同一批真实 DirEntry 的名字倒序迭代器，其它路径/FD 原样透传。
    # 类用途: 提供分页器需要的迭代与 close 接口。
    class _ReverseNameEntries:
        def __init__(self, entries):
            self._entries = iter(sorted(entries, key=lambda entry: entry.name, reverse=True))

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

        def close(self):
            self._entries = iter(())

    # LLM: 真实列举后立即关闭原迭代器，不改变目录内容或 DirEntry 的类型判断。
    # 函数用途: 固定 agents 根的扫描顺序，消除文件系统目录序差异。
    def reverse_name_scandir(target=".", *args, **kwargs):
        if isinstance(target, str | os.PathLike) and Path(target) == agents_root:
            with real_scandir(target) as entries:
                return _ReverseNameEntries(list(entries))
        return real_scandir(target, *args, **kwargs)

    monkeypatch.setattr(locator_hygiene.os, "scandir", reverse_name_scandir)
    quiet = home / "agents" / quiet_name / "state.json"
    quiet.parent.mkdir(parents=True)
    path.rename(quiet)
    runtime.record_run_creation(owner_id="local/main", goal="active parent", run_id="active-root", role="main")
    runtime.record_run_creation(owner_id="local/main", goal="active sibling", run_id=active_name,
                                role="worker", parent_run_id="active-root")
    active = home / "agents" / active_name / "state.json"
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({"run_id": active_name, "status": "RUNNING"}), encoding="utf-8")
    original, visited = discovery._blocked_subagent_is_quiescent, []

    # LLM: 保留原SQL判据，只记录静止项是否在活跃兄弟之前被真正读取。
    # 函数用途: 核对倒序扫描确实穿过静止子代理。
    def observed(owner_home, run_id, payload):
        result = original(owner_home, run_id, payload)
        visited.append(result)
        return result

    monkeypatch.setattr(discovery, "_blocked_subagent_is_quiescent", observed)
    assert _has_unfinished_subagent_run(home) is True
    assert [owner.owner_id for owner in discover_wake_pending_owners(owners)] == ["quiet-group"]
    if quiet_name.startswith("z_"):
        assert visited and all(visited)


# LLM: 原发现入口在账本检查内直接补关TaskRun；已收口无需额外唤醒，不能为满足预期新增第二条结算链。
# 函数用途: 验证静止子代理让出发现顺序后，父TaskRun经原账本入口收口且下轮保持安静。
def test_b4_quiescent_child_with_open_task_run_falls_through_to_task_settle(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    owners, home, runtime, tree, path = _b4_quiet_owner(tmp_path, closed=False)
    assert _task_run(runtime, tree["task_run_id"])["closed_at"] == 0
    assert _has_unfinished_subagent_run(home) is False
    original, observed = discovery._has_unfinished_task_ledger, []
    state_bytes = path.read_bytes()

    # LLM: 不替换账本返回值或收口协议，必须执行真实unfinished_task_ids及TaskRun CAS。
    # 函数用途: 记录发现扫描确实落到该owner的未完结任务账本。
    def ledger(owner_home, **kwargs):
        observed.append((owner_home, _task_run(runtime, tree["task_run_id"])["closed_at"]))
        return original(owner_home, **kwargs)

    monkeypatch.setattr(discovery, "_has_unfinished_task_ledger", ledger)
    assert discover_wake_pending_owners(owners) == []
    assert observed and observed[0] == (home, 0)
    assert unfinished_task_ids(home) == []
    assert _task_run(runtime, tree["task_run_id"])["closed_at"] > 0
    closed, = _events(runtime, tree["task_run_id"], "task_run.closed")
    assert closed["payload"]["operator"] == "wake-discovery-task-run-reconcile"
    assert closed["payload"]["quiescent_agent_run_ids"] == [tree["child"]["agent_run_id"]]
    assert discover_wake_pending_owners(owners) == []
    assert path.read_bytes() == state_bytes


# LLM: 现代WAL模式库即使没有sidecar也不能因只读观察重新创建wal/shm；观察每条SQL之间的目录，防短暂创建后删除掩盖副作用。
# 函数用途: 验证冷现代库不经repo初始化，并支持任务data布局与原权威根布局。
@pytest.mark.parametrize("layout", ["root", "data"])
def test_b4_cold_wal_probe_does_not_create_sidecars_or_use_writable_repo(tmp_path, monkeypatch, layout):
    import sqlite3
    from contextlib import closing

    from agent_py_agent.agent import owner_wake_discovery as discovery

    _owners, _home, runtime, _tree_row, state = _b4_quiet_owner(tmp_path / "source")
    home = tmp_path / "probe"
    path = home / "agents" / "child-1" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(state.read_bytes())
    db = home / "data/runtime.db" if layout == "data" else home / "runtime.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(runtime._runtime_connect()) as source, closing(sqlite3.connect(db)) as target:
        source.backup(target)
        assert target.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()
    before = db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in db.parent.iterdir())
    original, seen = sqlite3.connect, []

    # LLM: 真实sqlite连接保留SQL行为，连接参数和执行期间目录均要证明只读。
    # 函数用途: 捕获连接模式与短暂sidecar创建。
    def connect(address, *args, **kwargs):
        assert kwargs.get("uri") is True and "mode=ro" in str(address)
        conn = original(address, *args, **kwargs)
        conn.set_trace_callback(lambda sql: seen.append((sql, sorted(p.name for p in db.parent.iterdir()))))
        return conn

    # LLM: 该探针不能借用写仓库缓存，哪怕初始化在schema相同情况下看似不改业务表。
    # 函数用途: 拦截被禁止的可写repo入口。
    def writable_repo(_home):
        pytest.fail("quiescent observation must not call writable repository")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(discovery, "_runtime_repo_for_owner", writable_repo)
    assert _has_unfinished_subagent_run(home) is False
    assert seen and all(not name.endswith(("-wal", "-shm")) for _sql, names in seen for name in names)
    assert (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in db.parent.iterdir())) == before


# LLM: DELETE模式必须遵守SQLite读锁，不能为禁建WAL而对全部旧库使用immutable绕开活跃写事务。
# 函数用途: 验证旧布局权威被独占锁定时保守继续唤醒，不拿可能变化的无锁快照证明静止。
def test_b4_locked_delete_journal_database_keeps_waking(tmp_path):
    import sqlite3
    from contextlib import closing

    _owners, _home, runtime, _tree_row, state = _b4_quiet_owner(tmp_path / "source")
    home = tmp_path / "probe"
    path = home / "agents" / "child-1" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(state.read_bytes())
    db = home / "runtime.db"
    with closing(runtime._runtime_connect()) as source, closing(sqlite3.connect(db)) as target:
        source.backup(target)
        assert target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    with closing(sqlite3.connect(db)) as locker:
        locker.execute("BEGIN EXCLUSIVE")
        try:
            assert _has_unfinished_subagent_run(home) is True
        finally:
            locker.rollback()


# LLM: data兼容分支也必须拒绝owner本身的符号链接，不得通过普通data父目录绕过直接父目录检查。
# 函数用途: 复现独立审查指出的兼容布局链接隔离缺口。
def test_b4_data_probe_rejects_symlinked_owner_home(tmp_path):
    _owners, home, _runtime, _tree_row, _path = _b4_quiet_owner(tmp_path, data_layout=True)
    alias = tmp_path / "owner-alias"
    alias.symlink_to(home, target_is_directory=True)
    assert _has_unfinished_subagent_run(alias) is True


# LLM: 已选data兼容库之后若现行根权威出现，就不能继续用旧兼容库压掉唤醒。
# 函数用途: 在确定的快照边界安装新的根权威，复现独立审查指出的权威切换窗口。
def test_b4_data_probe_keeps_waking_if_primary_authority_appears(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    _owners, home, runtime, _tree_row, _path = _b4_quiet_owner(tmp_path, data_layout=True)
    primary, original = home / "runtime.db", discovery._file_signature
    wal = runtime.db_path.with_name(runtime.db_path.name + "-wal")

    # LLM: 外部写模拟只发生一次，原签名与探针查询均真实执行，不伪造查询结果。
    # 函数用途: 在读兼容库时模拟根权威安装。
    def signature(path):
        result = original(path)
        if path == wal and not primary.exists():
            primary.write_bytes(b"new primary authority requires recovery")
        return result

    monkeypatch.setattr(discovery, "_file_signature", signature)
    assert _has_unfinished_subagent_run(home) is True
    assert primary.is_file()
