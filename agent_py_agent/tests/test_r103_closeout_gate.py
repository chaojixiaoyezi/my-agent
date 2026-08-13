"""R1-03 收口闸红测（v5 基线：先红后实现）。

根因（已坐实）：resource_locks 租约 API（acquire_locks/renew_lock/expired_locks）
在生产代码零调用点——G.6 租约机制建成但从未接线。create_attempt 不查 run 终态、
不查执行权锁；发现层只读 state.json 与账本，runtime.db 终态不参与；驱动链无
lease 感知——自喂循环：settle 置 run 终态 → 发现层仍见 RUNNING → 每 cooldown
挂新 running attempt 到 done run → 新 attempt 永不收口 → running 残留 → 继续
驱动（真机：agentrun-1786466638 run=done 仍被驱动 13 次）。

v5 设计（六轮评审闭合）：
  - AGENT_RUN_TERMINAL_STATUSES={done,failed,cancelled} 集中常量，''→created 兼容
  - create_attempt 单事务：终态闸 + 执行权锁三态（缺失/活跃拒/过期或持主死亡接管）
  - settle 绑定 current attempt（stale 拒 + closeout_blocked 诊断事件）
  - 发现层 runtime.db 终态权威（stale state.json 降级为冲突观测 + status_conflict 事件）
  - 驱动链 lease 感知（有锁不催/无锁非终态催/终态不催）
  - 收口矩阵五档 + 孤儿兜底四件套（判 failed 前置副作用门）
  - 未知状态四处 fail-closed（不写 settled/completed）

约定：标 ``# RED`` 的用例当前必须失败（先红）；实现后全部转绿。
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids
from agent_py_agent.agent.runtime_db.repository import (
    RuntimeConflictError,
    RuntimeRepository,
    holder_is_alive,
)
from agent_py_agent.agent.runtime_db.schema import runtime_db_path

TERMINAL_STATUSES = {"done", "failed", "cancelled"}


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(runtime_db_path(tmp_path))


def _exec_scope(agent_run_id: str) -> str:
    return f"attempt-exec:{agent_run_id}"


def _other_repo(tmp_path):
    """模拟另一进程的独立仓库实例（instance_id 显式不同——同进程内默认
    hostname-pid 相同，无法区分两个实例）。"""
    return RuntimeRepository(runtime_db_path(tmp_path), instance_id="other-host-other-pid")


def _make_task_with_run(repo, *, run_status: str = "created"):
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    run = repo.create_agent_run(task_run_id=task_run["task_run_id"], role="main")
    if run_status != "created":
        conn = repo._runtime_connect()
        try:
            conn.execute(
                "UPDATE agent_runs SET status = ? WHERE agent_run_id = ?",
                (run_status, run["agent_run_id"]),
            )
            conn.commit()
        finally:
            conn.close()
    return task, task_run, run


def _insert_exec_lock(repo, *, agent_run_id: str, holder_instance: str,
                      attempt_id: str, generation: int,
                      lease_expires_at: float, pid: int = 0,
                      start_token: str = "") -> None:
    """手搓一条执行权锁（scope=attempt-exec:{run_id}），绕过尚未实现的 API。

    替换持主语义：同 scope 已有锁（如 create_attempt 刚建的）先删再插，
    测试即「把持主换成 dead-holder」。
    """
    import uuid

    conn = repo._runtime_connect()
    try:
        conn.execute(
            "DELETE FROM resource_locks WHERE canonical_scope = ?",
            (_exec_scope(agent_run_id),),
        )
        conn.execute(
            """
            INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance,
                                       pid, start_token, attempt_id, attempt_generation,
                                       workspace_epoch, tool_operation_generation,
                                       lease_expires_at, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
            """,
            (uuid.uuid4().hex, _exec_scope(agent_run_id), holder_instance,
             pid, start_token, attempt_id, generation, lease_expires_at,
             time.time(), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_operation(repo, *, attempt_id: str, status: str,
                      side_effect: bool = True, effect_key: str = "",
                      attempt_generation: int = 1) -> None:
    """手搓一条 tool_operations 行（op 状态矩阵喂料）。"""
    import uuid

    payload = {
        "side_effect": side_effect,
    }
    if effect_key:
        payload["effect_key"] = effect_key
    conn = repo._runtime_connect()
    try:
        conn.execute(
            """
            INSERT INTO tool_operations(operation_id, attempt_id, agent_run_id,
                                        attempt_generation, tool_operation_generation,
                                        operation_type, status, handler_started_at,
                                        outcome_json, created_at, updated_at)
            VALUES(?, ?, '', ?, 0, 'test', ?, 1, ?, ?, ?)
            """,
            (uuid.uuid4().hex, attempt_id, attempt_generation, status,
             json.dumps(payload, ensure_ascii=False), time.time(), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _events_of_type(repo, event_type: str) -> list:
    conn = repo._runtime_connect()
    try:
        rows = conn.execute(
            "SELECT * FROM runtime_events WHERE event_type = ?", (event_type,)
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


# =================================================================== A. 挂载闸
# RED：create_attempt 挂到未知状态 run 必须 fail-closed 拒绝（当前静默挂载 →
# 自喂循环的账本错乱放大）；终态（done/failed/cancelled）挂载放行——自喂
# 防护在发现层终态过滤 + 执行权锁，任务级生命周期闸裁决「该不该重跑」（子
# 代理 followup 打回重跑、policy due 周期轮换、user-stop 恢复都是同 run 重挂
# 的合法显式调度）。

@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
def test_a_create_attempt_allows_remount_on_terminal_run(repo, terminal):
    """终态 run 可重挂 attempt（重试/打回重跑/周期轮换语义）。"""
    _, _, run = _make_task_with_run(repo, run_status=terminal)
    attempt = repo.create_attempt(run["agent_run_id"])
    assert attempt["attempt_generation"] == 1


def test_a_create_attempt_rejects_unknown_status(repo):  # RED
    """未知状态（历史 unfinished 等非法值）→ fail-closed 拒绝 + 诊断事件。"""
    _, _, run = _make_task_with_run(repo, run_status="unfinished")
    with pytest.raises(RuntimeConflictError):
        repo.create_attempt(run["agent_run_id"])
    assert len(_events_of_type(repo, "status_conflict")) == 1


def test_a_create_attempt_allowed_on_created_run(repo):
    """created（现役合法态）必须照常放行——本闸只挡终态。"""
    _, _, run = _make_task_with_run(repo, run_status="created")
    attempt = repo.create_attempt(run["agent_run_id"])
    assert attempt["attempt_generation"] == 1


def test_a_legacy_empty_status_is_created_compatible(repo):
    """旧 /ask 路径历史 '' 视为 created 兼容映射，不得误伤（迁移红线）。"""
    _, _, run = _make_task_with_run(repo, run_status="")
    attempt = repo.create_attempt(run["agent_run_id"])
    assert attempt["attempt_generation"] == 1


# =================================================================== B. 执行权锁三态
# 缺失 → 建锁；活跃（他人持有）→ 拒；过期或持主死亡 → 接管。

def test_b_create_attempt_takes_exec_lock(repo):
    """创建 attempt 即持有执行权锁 scope=attempt-exec:{run_id}（锁归属新 attempt）。"""
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    lock = repo.lock_for_scope(_exec_scope(run["agent_run_id"]))
    assert lock is not None
    assert lock["attempt_id"] == attempt["attempt_id"]
    assert int(lock["attempt_generation"]) == 1
    assert lock["holder_instance"] == repo.instance_id


def test_b_create_attempt_rejects_active_foreign_lock(repo, tmp_path):  # RED
    """活跃执行权锁（未过期、持主存活、非本实例）→ 拒绝（并发双挂载恰一成功）。"""
    _, _, run = _make_task_with_run(repo)
    first = repo.create_attempt(run["agent_run_id"])
    other = _other_repo(tmp_path)  # 模拟另一进程（独立 holder）
    # 另一进程对同一 run 并发挂载 → 必须拒
    with pytest.raises(RuntimeConflictError):
        other.create_attempt(run["agent_run_id"])
    # 且 current pointer 未被触碰
    assert repo.current_attempt(run["agent_run_id"])["attempt_id"] == first["attempt_id"]


def test_b_same_instance_rotation_allowed(repo):
    """同 holder 换代（compact/续跑轮）→ 放行，锁归属换代后的新 attempt。"""
    _, _, run = _make_task_with_run(repo)
    first = repo.create_attempt(run["agent_run_id"])
    second = repo.create_attempt(run["agent_run_id"])
    assert second["attempt_generation"] == 2
    lock = repo.lock_for_scope(_exec_scope(run["agent_run_id"]))
    assert lock["attempt_id"] == second["attempt_id"]
    assert lock["holder_instance"] == repo.instance_id


def test_b_create_attempt_takes_over_expired_lock(repo, tmp_path):  # RED
    """过期执行权锁 → 接管（删旧锁建新锁），新 attempt 成为唯一持锁者。"""
    _, _, run = _make_task_with_run(repo)
    first = repo.create_attempt(run["agent_run_id"])
    _expire_lock(repo, run["agent_run_id"])
    other = _other_repo(tmp_path)
    attempt = other.create_attempt(run["agent_run_id"])
    assert attempt["attempt_generation"] == 2
    lock = repo.lock_for_scope(_exec_scope(run["agent_run_id"]))
    assert lock["attempt_id"] == attempt["attempt_id"]
    assert lock["holder_instance"] == other.instance_id


def _expire_lock(repo, agent_run_id: str) -> None:
    conn = repo._runtime_connect()
    try:
        conn.execute(
            "UPDATE resource_locks SET lease_expires_at = ? "
            "WHERE canonical_scope = ?",
            (time.time() - 7200, _exec_scope(agent_run_id)),
        )
        conn.commit()
    finally:
        conn.close()


def test_b_create_attempt_takes_over_dead_holder_lock(repo, tmp_path):  # RED
    """持主进程已死（pid 不存在）→ 接管；僵尸锁不得阻塞恢复。"""
    _, _, run = _make_task_with_run(repo)
    first = repo.create_attempt(run["agent_run_id"])
    # 锁未过期但持主 pid 已死（进程不存在）→ 视为可接管
    _insert_exec_lock(repo, agent_run_id=run["agent_run_id"],
                      holder_instance="dead-holder",
                      attempt_id=first["attempt_id"], generation=1,
                      lease_expires_at=time.time() + 3600,
                      pid=999999, start_token="stale-token")
    other = _other_repo(tmp_path)
    attempt = other.create_attempt(run["agent_run_id"])
    assert attempt["attempt_generation"] == 2
    lock = repo.lock_for_scope(_exec_scope(run["agent_run_id"]))
    assert lock["holder_instance"] == other.instance_id


# =================================================================== C. 旧 worker 越 fence
# takeover（接管换代）后，旧 attempt 的 renew/settle 必须被拒且留痕。

def test_c_renew_without_held_lock_fails(repo):
    """未持有锁却 renew → 拒（renew 必须 CAS 命中本 holder 锁行，防回归绿测）。"""
    _, _, run = _make_task_with_run(repo)
    first = repo.create_attempt(run["agent_run_id"])
    with pytest.raises(RuntimeConflictError):
        repo.renew_lock(
            canonical_scope=_exec_scope(run["agent_run_id"]),
            holder_instance="not-the-holder",
            attempt_id=first["attempt_id"],
            attempt_generation=1,
        )


def test_c_settle_rejected_for_stale_attempt(repo, tmp_path):  # RED
    """run 已被接管换代后，旧 attempt 的 settle 必须拒（stale_attempt）+ 诊断事件。"""
    _, _, run = _make_task_with_run(repo)
    first = repo.create_attempt(run["agent_run_id"])
    # 锁过期 → 另一实例接管换代（活跃锁不会被接管——这正是 test_b 的拒）
    _expire_lock(repo, run["agent_run_id"])
    other = _other_repo(tmp_path)
    other.create_attempt(run["agent_run_id"])  # 接管换代（同库，第二实例）
    result = repo.settle_agent_run(
        agent_run_id=run["agent_run_id"],
        status="done",
        attempt_id=first["attempt_id"],  # 旧 attempt 自称收口
    )
    assert result["settled"] is False
    assert result["reason"] == "stale_attempt"
    assert _events_of_type(repo, "closeout_blocked")


def test_c_settle_by_current_attempt_allowed(repo):
    """当前 attempt 正常 settle 照旧（不传 attempt_id 的旧调用也兼容）。"""
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    result = repo.settle_agent_run(
        agent_run_id=run["agent_run_id"],
        status="done",
        attempt_id=attempt["attempt_id"],
    )
    assert result["settled"] is True
    assert repo.get_agent_run(run["agent_run_id"])["status"] == "done"


# =================================================================== D. 发现层 stale 屏蔽
# runtime.db 是终态单一权威：state.json 残留 RUNNING 但账本 run 已终态 → 排除
# （降级为冲突观测 status_conflict 事件，绝不参与驱动）。


def _make_owner_home_with_stale_state(tmp_path, repo, *, run_status: str,
                                      ledger_status: str = "RUNNING",
                                      with_exec_lock: bool = False):
    """构造 owner_home：link active + 账本 ledger_status + runtime.db 终态/锁。"""
    task, task_run, run = _make_task_with_run(repo, run_status=run_status)
    owner_home = Path(runtime_db_path(tmp_path)).parent
    link = owner_home / "workspace" / "runtime" / "workspaces" / "ws-1" \
        / "conversations" / "tasks" / f"{task['task_id']}.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text(json.dumps({"task_id": task["task_id"], "status": "active"}),
                    encoding="utf-8")
    ledger = owner_home / "tasks" / "2026-08-11" / "demo-task" / "work" / "state.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"task_id": task["task_id"], "status": ledger_status}),
                      encoding="utf-8")
    if with_exec_lock:
        _insert_exec_lock(repo, agent_run_id=run["agent_run_id"],
                          holder_instance="alive-holder",
                          attempt_id="attempt-lock-holder", generation=7,
                          lease_expires_at=time.time() + 3600,
                          pid=os.getpid(), start_token=repo._start_token())
    return task, run


def test_d_discovery_excludes_done_run_stale_state(tmp_path, repo):  # RED
    """run 已 done + state.json 残留 RUNNING → 发现层必须排除（不再驱动）。"""
    task, _run = _make_owner_home_with_stale_state(tmp_path, repo, run_status="done")
    assert task["task_id"] not in unfinished_task_ids(tmp_path)


def test_d_discovery_still_finds_running_run(tmp_path, repo):
    """非终态 run（created）+ RUNNING 账本 → 照常发现（不能把活任务误杀）。"""
    task, _run = _make_owner_home_with_stale_state(tmp_path, repo, run_status="created")
    assert task["task_id"] in unfinished_task_ids(tmp_path)


def test_d_discovery_no_status_conflict_for_done_stale_state(tmp_path, repo):
    """done 终态 + state.json 残留 → 正常排除, 不写 status_conflict。

    R1-03 收窄(bd27776a)后 status_conflict 只覆盖「投影失败」类真冲突
    (create_attempt 挂未知状态 run 等, 见 test_a 断言 len==1)——done 轮间
    形态是终态权威正常排除, 不洪泛诊断事件(修复前每秒 2 条无限增长)。
    """
    task, _run = _make_owner_home_with_stale_state(tmp_path, repo, run_status="done")
    unfinished_task_ids(tmp_path)
    assert not _events_of_type(repo, "status_conflict"), \
        f"task={task['task_id']} done 轮间形态不应写 status_conflict"


# =================================================================== E. 驱动链 lease 感知
# 有活跃执行权锁（worker 在跑）→ 不催；无锁非终态 → 催；终态 → 不催。

def test_e_discovery_excludes_task_with_active_exec_lock(tmp_path, repo):  # RED
    """run 非终态但已有活跃执行权锁（worker 在跑）→ 发现层不重复催。"""
    task, _run = _make_owner_home_with_stale_state(
        tmp_path, repo, run_status="created", with_exec_lock=True)
    assert task["task_id"] not in unfinished_task_ids(tmp_path)


# =================================================================== F. 收口矩阵五档
# 无 op 痕迹 → cancelled（纯账本）；全 SUCCEEDED → done；含 FAILED → failed；
# 非终态/UNKNOWN op → 停手（None，不自动裁决）。

def test_f_classify_no_ops_cancelled(repo):  # RED
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    assert repo.classify_attempt_closeout(attempt["attempt_id"]) == "cancelled"


def test_f_classify_all_succeeded_done(repo):  # RED
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    _insert_operation(repo, attempt_id=attempt["attempt_id"], status="SUCCEEDED")
    _insert_operation(repo, attempt_id=attempt["attempt_id"], status="SUCCEEDED")
    assert repo.classify_attempt_closeout(attempt["attempt_id"]) == "done"


def test_f_classify_has_failed_failed(repo):  # RED
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    _insert_operation(repo, attempt_id=attempt["attempt_id"], status="SUCCEEDED")
    _insert_operation(repo, attempt_id=attempt["attempt_id"], status="FAILED")
    assert repo.classify_attempt_closeout(attempt["attempt_id"]) == "failed"


def test_f_classify_nonterminal_stops(repo):  # RED
    """CLAIMED/EXECUTING/UNKNOWN 任一存在 → 停手（None），绝不自动收口。"""
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    _insert_operation(repo, attempt_id=attempt["attempt_id"], status="EXECUTING")
    assert repo.classify_attempt_closeout(attempt["attempt_id"]) is None
    _insert_operation(repo, attempt_id=attempt["attempt_id"], status="UNKNOWN")
    assert repo.classify_attempt_closeout(attempt["attempt_id"]) is None


# =================================================================== G. 孤儿兜底四件套
# 1) 锁过期+宽限期；2) 持主判死（pid+start_token 比对防 PID 复用）；
# 3) run 非终态；4) 判 failed 前置副作用门（有副作用 op 且无 effect_key → 停手）。

def test_g_find_orphaned_attempts(repo):  # RED
    """锁过期（超出宽限期）且 run 非终态的 attempt → 列为孤儿。"""
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    _insert_exec_lock(repo, agent_run_id=run["agent_run_id"],
                      holder_instance="dead-holder",
                      attempt_id=attempt["attempt_id"], generation=1,
                      lease_expires_at=time.time() - 3600,
                      pid=999999, start_token="tok")
    orphans = repo.find_orphaned_attempts(grace_seconds=600)
    assert any(o["attempt_id"] == attempt["attempt_id"] for o in orphans)


def test_g_active_lock_not_orphan(repo):
    """锁未过期 → 不是孤儿（即使宽限期已过也不能误杀活 worker）。"""
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    _insert_exec_lock(repo, agent_run_id=run["agent_run_id"],
                      holder_instance="alive-holder",
                      attempt_id=attempt["attempt_id"], generation=1,
                      lease_expires_at=time.time() + 3600,
                      pid=os.getpid(), start_token="tok")
    orphans = repo.find_orphaned_attempts(grace_seconds=0)
    assert not any(o["attempt_id"] == attempt["attempt_id"] for o in orphans)


def test_g_holder_liveness_start_token(repo):  # RED
    """持主判死：pid 不存在 → 死；pid 存活但 start_token 失配（PID 复用）→ 死。"""
    assert holder_is_alive(pid=999999, start_token="whatever") is False
    if platform.system() == "Linux":
        # PID 复用防护：start_token 对不上 → 视为不可信（fail-closed 判死）
        assert holder_is_alive(pid=os.getpid(), start_token="wrong-token") is False
        # 空 token 且进程在 → 存活
        assert holder_is_alive(pid=os.getpid(), start_token="") is True


def test_g_reclaim_side_effect_gate(repo):  # RED
    """孤儿 op 有外部副作用且无 effect_key → 拒自动判 failed（交人工，fail-closed）。"""
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    _insert_operation(repo, attempt_id=attempt["attempt_id"],
                      status="EXECUTING", side_effect=True, effect_key="")
    _insert_exec_lock(repo, agent_run_id=run["agent_run_id"],
                      holder_instance="dead-holder",
                      attempt_id=attempt["attempt_id"], generation=1,
                      lease_expires_at=time.time() - 3600,
                      pid=999999, start_token="tok")
    result = repo.reclaim_orphaned_attempt(attempt["attempt_id"],
                                           operator="test-red")
    assert result["reclaimed"] is False
    assert result["reason"] == "side_effect_gate"


def test_g_reclaim_no_side_effect_ok(repo):  # RED
    """孤儿无外部副作用（纯账本/可幂等）→ 按矩阵自动收口（cancelled）。"""
    _, _, run = _make_task_with_run(repo)
    attempt = repo.create_attempt(run["agent_run_id"])
    _insert_exec_lock(repo, agent_run_id=run["agent_run_id"],
                      holder_instance="dead-holder",
                      attempt_id=attempt["attempt_id"], generation=1,
                      lease_expires_at=time.time() - 3600,
                      pid=999999, start_token="tok")
    result = repo.reclaim_orphaned_attempt(attempt["attempt_id"],
                                           operator="test-red")
    assert result["reclaimed"] is True
    assert result["status"] == "cancelled"


# =================================================================== H. 未知状态 fail-closed
# 未知状态（含历史 unfinished 遗留非法值）四处 fail-closed，不写 settled/completed。

def test_h_create_attempt_rejects_unknown_status(repo):  # RED
    """status=unfinished（历史遗留非法值）→ 拒绝挂载 + status_conflict 事件。"""
    _, _, run = _make_task_with_run(repo, run_status="unfinished")
    with pytest.raises(RuntimeConflictError):
        repo.create_attempt(run["agent_run_id"])
    assert _events_of_type(repo, "status_conflict")


def test_h_settle_unknown_status_fail_closed(repo):  # RED
    """run 状态未知 → settle 不写 settled，留 status_conflict 诊断事件。"""
    _, _, run = _make_task_with_run(repo, run_status="unfinished")
    result = repo.settle_agent_run(agent_run_id=run["agent_run_id"], status="done")
    assert result["settled"] is False
    assert result["reason"] == "unknown_status"
    assert _events_of_type(repo, "status_conflict")
    # 权威账本不被污染：终态仍是未知态
    assert repo.get_agent_run(run["agent_run_id"])["status"] == "unfinished"


# =================================================================== I. 隔离域
# scheduler 是独立域：claim 后 agent_runs/agent_attempts 零写（防回归绿测）。

def test_i_scheduler_claim_isolated(tmp_path, repo):
    """scheduler（独立 JSON 存储）claim 全程不碰 agent_runs/agent_attempts。"""
    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRepository,
        SchedulerRunFinish,
    )

    sched = SchedulerRepository(
        tmp_path / "scheduler",
        owner_provider="local",
        owner_kind="main",
        owner_id="local/main",
        default_timezone="UTC",
    )
    sched.create_job(SchedulerJobCreateRequest(
        name="sched-demo", prompt="demo", thread_id="thread-1",
        source_task_id="task-1", source_request_id="req-demo",
        schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000},
        now=900,
    ))
    run = sched.reserve_due_runs(now=1_001)[0]
    claimed = sched.claim_run(str(run["run_id"]), lease_seconds=10, now=1_002)
    assert claimed is not None
    before = (repo._runtime_connect().execute(
        "SELECT COUNT(*) FROM agent_runs").fetchone()[0],
        repo._runtime_connect().execute(
        "SELECT COUNT(*) FROM agent_attempts").fetchone()[0])
    sched.finish_run(str(run["run_id"]), str(claimed["claim_id"]),
                     SchedulerRunFinish(status="done", now=1_020))
    after = (repo._runtime_connect().execute(
        "SELECT COUNT(*) FROM agent_runs").fetchone()[0],
        repo._runtime_connect().execute(
        "SELECT COUNT(*) FROM agent_attempts").fetchone()[0])
    assert after == before
