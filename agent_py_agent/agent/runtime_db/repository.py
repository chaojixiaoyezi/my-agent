# LLM: 任务执行权与 current CAS 共用原 RuntimeDB；精确预留/激活沿原身份，取消保留原领取元数据，联测严格回读、UNKNOWN 与换代。
# 模块用途: 保存任务、执行轮和委托关系，以事务方式领取和收回执行权。
"""Owner runtime.db 权威实体仓储（3.txt A/C/D/F 节落地面）。

职责：
- 主链（A.4）：Task → TaskRun → AgentRun 树 → AgentAttempt 的创建与读取。
- 不可变委托（A.7）：child 必须先建自己的 AgentAttempt 才能被 delegate。
- current attempt pointer（F.2/F.3）：create_attempt 以 CAS 递增 generation
  并替换 agent_runs.current_attempt_id；旧 attempt 的身份与执行事实不可改写，
  但其生命周期必须在换代事务内收口，不能留下非 current 的幽灵 RUNNING。
- WorkspaceBinding / root claims（D 节）：绑定承载可读写根集合与 epoch，
  同一物理写根在库内唯一声明（D.5）。
- runtime_events（A.3）：append-only 权威事件流，每事件追到 attempt（A.8）。

所有 ID 由 B.1 统一生成器（common.id_generator.new_id）铸造，本模块不手拼。
"""

# LLM: runtime.db 只保存 Task 身份与 TaskRun/AgentRun/Attempt 的执行生命周期；
# 禁止把已退休的机器验收合同或 Task 业务完成状态重新接回这个权威仓储。
# 冷 owner 的精确恢复与启动扫描共用进程死亡证明，展示投影不得覆盖 current attempt。
# 换代清理保留已确认 STABLE 资源，不把旧执行失去权限等同于其已完成副作用变得未知。
# 取消以同一写事务核对原 attempt/status；UNKNOWN 的执行锁与恢复障碍不能随资源停止清除。
# 模块用途: 管理 owner 级任务身份、每次执行、代理树、工具操作和恢复状态。

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..common.id_generator import new_id
from ..common.strict_json import load_strict_json

# LLM: 与 scheduler P0-4 共用同一套进程死亡证明判定（RUN-01），禁止另写第二份判死实现。
from ..scheduler.repository import (
    _process_start_time as _proc_start_time,
)
from ..scheduler.repository import (
    _process_state as _proc_state,
)
from .delivery_operations import RuntimeDeliveryMixin
from .host_commands import (
    HostCommandBinding,
    HostCommandRequest,
    insert_host_command,
    read_host_command,
)
from .operations import (
    _ATTEMPT_TERMINAL_STATUSES,
    AGENT_RUN_TERMINAL_STATUSES,
    ATTEMPT_STATUS_PENDING,
    ATTEMPT_STATUS_RECOVERED,
    ATTEMPT_STATUS_UNKNOWN,
    EXEC_LOCK_GRACE_SECONDS,
    EXEC_LOCK_LEASE_SECONDS,
    EXEC_LOCK_SCOPE_PREFIX,
    OP_CANCELLED,
    OP_CLAIMED,
    OP_EXECUTING,
    OP_FAILED,
    OP_SUCCEEDED,
    OP_UNKNOWN,
    RUN_STATUS_LEGACY_CREATED,
    RuntimeConflictError,
    RuntimeExecutionBusyError,
    RuntimeOperationsMixin,
    exec_lock_scope,
    holder_is_alive,
)
from .run_creation import RunCreation, create_run_chain
from .schema import RuntimeSchemaMixin, runtime_db_path

#: D.6/D.9：binding 生命周期状态。v1 不扩容（D.8），迁移时旧 ACTIVE → SUPERSEDED。
BINDING_ACTIVE = "ACTIVE"
BINDING_SUPERSEDED = "SUPERSEDED"

#: D.7：可写根集合 digest 不同但包含/交叉 → 拒绝（BINDING_ROOT_OVERLAP）。
BINDING_ROOT_OVERLAP = "BINDING_ROOT_OVERLAP"

#: D.5：同一物理写根已被其他 binding 声明 → 冲突。
ROOT_CLAIM_CONFLICT = "ROOT_CLAIM_CONFLICT"

# RuntimeConflictError 统一由 operations 模块定义（本模块 import 复用，
# 勿重复定义同名类——会遮蔽 operations 抛出的异常类导致调用方捕获不到）。


# LLM: 主代理自动挂载与后台调度必须共用这个纯结构化闸；新增状态时同步检查
# create_attempt 与 main_agent_recovery_block_for_task，禁止调用方解析异常文案。
# 函数用途: 根据 run/attempt 的权威状态返回是否必须先人工恢复。
def _main_agent_recovery_reason(run_status: str, attempt_status: str) -> str:
    normalized_run = str(run_status or "")
    if normalized_run not in RUN_STATUS_LEGACY_CREATED and \
            normalized_run not in AGENT_RUN_TERMINAL_STATUSES:
        return "unknown_run_status"
    if str(attempt_status or "") == ATTEMPT_STATUS_UNKNOWN:
        return "attempt_unknown_terminal"
    return ""


# LLM: 崩溃调和（RUN-01）依赖 attempt 记录的 runner 身份；start_time 仅 Linux /proc 可读，
# 其他平台为 None（判死时仅做 pid 探活，fail-closed 不猜）。
# 函数用途: 构造当前进程身份元数据（pid + 启动时刻），随 attempt 落账供崩溃恢复使用。
def _runner_identity_metadata() -> dict[str, object]:
    return {
        "runner_pid": os.getpid(),
        "runner_start_time": _proc_start_time(os.getpid()),
    }


# LLM: CLI startup and exact owner-turn recovery share the same process-death proof and
# current-attempt CAS. Missing/unverifiable process identity never authorizes takeover.
# 函数用途: 证实原进程已死后，将仍是 current 的运行轮记为 unknown；不按时长猜死，也不动新执行者。
def _mark_dead_runner_attempt_unknown(conn, row, current: float) -> bool:
    try:
        meta = json.loads(str(row["metadata_json"] or "{}"))
        pid = int(meta.get("runner_pid") or 0)
        if pid <= 0:
            return False
        state = _proc_state(pid)
        if state != "dead":
            if state != "alive" or meta.get("runner_start_time") is None:
                return False
            live_start = _proc_start_time(pid)
            if live_start is None or live_start == float(meta["runner_start_time"]):
                return False
    except (AttributeError, TypeError, ValueError):
        return False
    attempt_id = str(row["current_attempt_id"] or "")
    agent_run_id = str(row["agent_run_id"] or "")
    changed = conn.execute(
        "UPDATE agent_attempts SET status='unknown', ended_at=? WHERE attempt_id=? "
        "AND status IN ('running','created') AND EXISTS "
        "(SELECT 1 FROM agent_runs WHERE agent_run_id=? AND current_attempt_id=?)",
        (current, attempt_id, agent_run_id, attempt_id),
    ).rowcount
    if changed != 1:
        return False
    conn.execute(
        "UPDATE agent_runs SET status='unknown', updated_at=? "
        "WHERE agent_run_id=? AND current_attempt_id=?",
        (current, agent_run_id, attempt_id),
    )
    return True


# LLM: 原收口事务只取消精确作用域内 CLAIMED 且 handler 未启动的行；保留领取身份、输入和资源元数据，联测三个调用方及严格回读。
# 函数用途: 结束未执行的工具占位；损坏结果原文保留，不妨碍撤回已证实未启动操作的执行权。
def _cancel_unstarted_tool_operations(
    conn: sqlite3.Connection,
    *,
    agent_run_id: str,
    now: float,
    attempt_id: str = "",
) -> None:
    """Cancel CLAIMED operations whose handler provably never started."""
    scope_sql = "attempt_id = ?" if attempt_id else "agent_run_id = ?"
    scope_value = attempt_id or agent_run_id
    rows = conn.execute(
        "SELECT operation_id, operation_type, outcome_json FROM tool_operations "
        f"WHERE {scope_sql} AND status = ? AND handler_started_at = 0",
        (scope_value, OP_CLAIMED),
    ).fetchall()
    for row in rows:
        result_json = _unstarted_cancellation_payload(row["outcome_json"], row["operation_type"])
        conn.execute(
            "UPDATE tool_operations SET status = ?, settled_at = ?, outcome_json = ?, updated_at = ? "
            "WHERE operation_id = ? AND status = ? AND handler_started_at = 0",
            (OP_CANCELLED, now, result_json, now, str(row["operation_id"]), OP_CLAIMED),
        )


# LLM: 这里只拥有取消结果字段；仅处理原 TEXT JSON，SQLite 非文本值、过深或坏内容保留，严格读取端拒绝；原领取身份不改。
# 函数用途: 给合法领取记录追加未启动取消回执，不修改磁盘；不可读内容交回原字节供后续诊断。
def _unstarted_cancellation_payload(raw: str | bytes, tool: str) -> str | bytes:
    if not isinstance(raw, str):
        return raw
    try:
        payload = load_strict_json(raw)
    except (TypeError, ValueError, RecursionError):
        return raw
    if not isinstance(payload, dict) or payload.get("schema", "managed_operation.v1") != "managed_operation.v1":
        return raw
    payload.update(
        schema="managed_operation.v1",
        result={
            "schema_version": "tool_execution_result.v1", "tool": str(tool or ""), "ok": False,
            "output": "未启动(not_started): 执行轮已结束, 操作从未执行, 无副作用(G.5 CANCELLED)",
            "error_code": "TOOL_OPERATION_CANCELLED_NOT_STARTED", "effect_outcome": "not_started",
            "handler_executed": False,
        },
        error_code="TOOL_OPERATION_CANCELLED_NOT_STARTED",
        unknown_reason="",
    )
    return json.dumps(payload, ensure_ascii=False)


# LLM: Manual and typed active-turn recovery must share this one unknown->recovered CAS.  The
# caller owns effect verification; this helper only performs the exact current-attempt transition,
# run repair, lock release and append-only event in the caller's transaction.
# 函数用途: 在副作用已经由上层结构化核对后，原子恢复当前 unknown 执行轮并释放执行锁。
def _recover_unknown_attempt_conn(
    repository: Any,
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    operator: str,
    effect_disposition: str,
    reason: str,
    event_facts: dict[str, object] | None = None,
) -> dict[str, Any]:
    now = time.time()
    row = conn.execute(
        "SELECT aa.agent_run_id, ar.current_attempt_id, ar.status AS run_status "
        "FROM agent_attempts aa "
        "JOIN agent_runs ar ON ar.agent_run_id = aa.agent_run_id "
        "WHERE aa.attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return {"recovered": False, "reason": "no_such_attempt"}
    if str(row["current_attempt_id"] or "") != str(attempt_id):
        return {"recovered": False, "reason": "not_current_attempt"}
    cur = conn.execute(
        "UPDATE agent_attempts SET status = ?, ended_at = ? "
        "WHERE attempt_id = ? AND status = ? "
        "AND EXISTS (SELECT 1 FROM agent_runs ar "
        "WHERE ar.agent_run_id = agent_attempts.agent_run_id "
        "AND ar.current_attempt_id = agent_attempts.attempt_id)",
        (ATTEMPT_STATUS_RECOVERED, now, attempt_id, ATTEMPT_STATUS_UNKNOWN),
    )
    if cur.rowcount != 1:
        return {"recovered": False, "reason": "not_unknown"}
    agent_run_id = str(row["agent_run_id"] or "")
    run_status_before = str(row["run_status"] or "")
    run_status_after = run_status_before
    if run_status_before == ATTEMPT_STATUS_UNKNOWN:
        conn.execute(
            "UPDATE agent_runs SET status = 'created', updated_at = ? "
            "WHERE agent_run_id = ? AND status = 'unknown'",
            (now, agent_run_id),
        )
        run_status_after = "created"
    conn.execute(
        "DELETE FROM resource_locks WHERE canonical_scope = ? AND attempt_id = ?",
        (exec_lock_scope(agent_run_id), attempt_id),
    )
    payload: dict[str, object] = {
        "status": ATTEMPT_STATUS_RECOVERED,
        "operator": str(operator or ""),
        "effect_disposition": effect_disposition,
        "reason": str(reason or ""),
        "run_status_before": run_status_before,
        "run_status_after": run_status_after,
    }
    payload.update(dict(event_facts or {}))
    repository._append_event_conn(
        conn,
        event_type="attempt_recovered",
        attempt_id=attempt_id,
        agent_run_id=agent_run_id,
        payload=payload,
    )
    return {
        "recovered": True,
        "attempt_id": attempt_id,
        "agent_run_id": agent_run_id,
        "run_status": run_status_after,
    }


# LLM: Active-turn archive facts are untrusted projections until matched to RuntimeDB rows; keep
# normalization limited to the two fields used by that comparison and discard empty identities.
# 函数用途: 规范化同一回合工具归档里的操作 ID、终态和工具名，供事务核对使用。
def _normalize_recorded_operation_facts(
    facts_by_id: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    return {
        str(operation_id or "").strip(): {
            "status": str((facts or {}).get("status") or "").strip().upper(),
            "operation_type": str((facts or {}).get("operation_type") or "").strip(),
        }
        for operation_id, facts in dict(facts_by_id or {}).items()
        if str(operation_id or "").strip()
    }


# LLM: Exact task+run equality is the anti-confusion boundary for automatic active-turn recovery;
# never fall back to the newest task run, thread id, prompt text, or an unrelated main row.
# Read PID/start metadata from that same current attempt, never from a sibling or stale generation.
# 函数用途: 读取精确 task/run 的根执行轮和同一 current attempt 的进程凭据，供原子恢复核对。
def _exact_active_turn_run(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ar.agent_run_id, ar.status AS run_status, "
        "ar.current_attempt_id, aa.status AS attempt_status, aa.metadata_json "
        "FROM agent_runs ar "
        "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id "
        "JOIN tasks t ON t.task_id = tr.task_id "
        "LEFT JOIN agent_attempts aa ON aa.attempt_id = ar.current_attempt_id "
        "WHERE t.task_id = ? AND ar.run_id = ? AND ar.role = 'main' "
        "AND ar.parent_agent_run_id = '' "
        "ORDER BY ar.created_at DESC LIMIT 1",
        (task_id, run_id),
    ).fetchone()


# LLM: A settled database outcome is not enough for model continuation: every started success or
# failure must also have its exact durable carried record. CLAIMED+never-started is the sole safe
# omission and is cancelled only after the whole preflight passes.
# 函数用途: 核对旧执行轮所有工具终态和耐久归档，返回可恢复性与安全取消数量。
def _active_turn_operation_recovery_report(
    conn: sqlite3.Connection,
    attempt_id: str,
    recorded: dict[str, dict[str, str]],
) -> dict[str, object]:
    operations = conn.execute(
        "SELECT operation_id, operation_type, status, handler_started_at, settled_at "
        "FROM tool_operations WHERE attempt_id = ? ORDER BY tool_operation_generation",
        (attempt_id,),
    ).fetchall()
    blockers: list[str] = []
    required: dict[str, tuple[str, str]] = {}
    unstarted_count = 0
    for operation in operations:
        operation_id = str(operation["operation_id"] or "")
        operation_type = str(operation["operation_type"] or "")
        status = str(operation["status"] or "").upper()
        handler_started_at = float(operation["handler_started_at"] or 0)
        settled_at = float(operation["settled_at"] or 0)
        if status == OP_CLAIMED and handler_started_at == 0:
            unstarted_count += 1
        elif status in {OP_SUCCEEDED, OP_FAILED} and settled_at > 0:
            required[operation_id] = (status, operation_type)
        elif not (status == OP_CANCELLED and handler_started_at == 0 and settled_at > 0):
            blockers.append(operation_id)
    if blockers:
        return {
            "ok": False,
            "reason": "operation_outcome_uncertain",
            "blocking_operation_ids": blockers,
        }
    missing: list[str] = []
    mismatched: list[str] = []
    for operation_id, (status, operation_type) in required.items():
        fact = recorded.get(operation_id)
        if fact is None:
            missing.append(operation_id)
        elif fact["status"] != status or fact["operation_type"] != operation_type:
            mismatched.append(operation_id)
    if missing or mismatched:
        return {
            "ok": False,
            "reason": "operation_record_incomplete",
            "missing_operation_ids": missing,
            "mismatched_operation_ids": mismatched,
        }
    return {
        "ok": True,
        "recorded_operation_count": len(required),
        "cancelled_unstarted_operation_count": unstarted_count,
    }


# LLM: Mutation state is independent of tool terminality; a DIRTY or MUTATING scope keeps the
# generic unknown hard stop even when every handler row and archive record otherwise matches.
# 函数用途: 列出指定旧执行轮仍不稳定的资源修改，空列表才允许自动恢复。
def _uncertain_attempt_mutation_ids(
    conn: sqlite3.Connection,
    attempt_id: str,
) -> list[str]:
    rows = conn.execute(
        "SELECT mutation_id FROM resource_mutations "
        "WHERE attempt_id = ? AND state != 'STABLE' ORDER BY mutation_id",
        (attempt_id,),
    ).fetchall()
    return [str(item["mutation_id"] or "") for item in rows]


# LLM: RuntimeRepository 是 owner runtime.db 的唯一写入口；新增状态前必须区分
# 长期 Task 身份与可停止、失败、恢复的 TaskRun/AgentRun/Attempt 执行状态。
# 类用途: 为 Gateway、主代理和恢复器提供同一份 SQLite 运行事实源。
class RuntimeRepository(
    RuntimeSchemaMixin,
    RuntimeOperationsMixin,
    RuntimeDeliveryMixin,
):
    """单 owner 权威库入口。每个 owner 一个实例（A.1）。"""

    def __init__(self, db_path: str | Path, *, instance_id: str = ""):
        self.db_path = Path(db_path)
        # R1-03：执行权锁 holder 身份（host-pid 实例级，跨进程天然互斥，
        # 同进程多次换代视为同一 worker 续跑——compact/账本续跑轮）。
        # instance_id 可选注入：测试模拟另一进程（同进程多实例 pid 相同，
        # 默认 identity 无法区分）。
        self.instance_id = instance_id or f"{socket.gethostname()}-{os.getpid()}"
        self._init_runtime_schema()

    # ------------------------------------------------------------------ 事务
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """多实体权威写入的原子事务（A.9：ConversationTaskLink 需同事务）。"""
        conn = self._runtime_connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -------------------------------------------- A.4/A.5/A.6/A.9 主链写入
    # LLM: 根运行创建新的 TaskRun，child 复用父 TaskRun；Task 本身只承载
    # owner/thread/goal 等耐久身份，不写本次执行状态。
    # 与宿主命令共用 run_creation 的同连接写入；本入口仍负责校验与事务，联测主代理及子代理创建。
    # 函数用途: 在一个事务里登记任务身份、运行、代理、首次尝试和委托关系。
    def record_run_creation(
        self,
        *,
        owner_id: str,
        goal: str = "",
        conversation_task_id: str = "",
        thread_id: str = "",
        run_id: str = "",
        role: str = "",
        parent_run_id: str = "",
        attempt_status: str = "running",
    ) -> dict[str, str]:
        """create_run 权威主链写入（单事务）。

        一个 TaskRun（=一次执行）→ 一棵 root/child AgentRun 树（A.4/A.5：
        一次 TaskRun 下一棵根树；每个 TaskRun 创建一个 root AgentRun）→
        每个 AgentRun 第一个 AgentAttempt（A.6：执行任何工具前必须有
        attempt）→ current pointer（F.3 初值 generation 1）。
        conversation_task_id 与 thread_id 在同一事务进入 tasks 行（A.9：
        ConversationTaskLink 不再是独立权威）。

        ``attempt_status=pending`` 用于只完成委托登记、尚未交给 runner 的
        子代理；真实 dispatcher 启动时必须原子激活同一个 generation 1，
        不能再造一条 running attempt。默认 running 保留“登记即执行”的主代理
        和底层直接调用语义。

        F9（A.4 树建模）：child（parent 有权威记录）并入 parent 的 TaskRun，
        不新建 TaskRun/Task——树的 AgentRun 同属一个 TaskRun，链是
        Task→TaskRun→AgentRun tree→AgentAttempt；child 经 immutable
        Delegation 连接 parent AgentRun（A.7）。parent 无权威记录
        （R1 前存量/旁路）时 child 自成新树根（parent/delegation 为空 =
        合法根身份 A.7），不阻断新链。
        """
        normalized_attempt_status = str(attempt_status or "").strip().lower()
        if normalized_attempt_status not in {ATTEMPT_STATUS_PENDING, "running"}:
            raise ValueError(f"不支持的初始 attempt 状态: {attempt_status!r}")
        with self.transaction() as conn:
            return create_run_chain(
                conn,
                RunCreation(
                    owner_id=owner_id, goal=goal, conversation_task_id=conversation_task_id,
                    thread_id=thread_id, run_id=run_id, role=role, parent_run_id=parent_run_id,
                    attempt_status=normalized_attempt_status,
                    attempt_metadata=(
                        _runner_identity_metadata()
                        if normalized_attempt_status == "running"
                        else {"lifecycle": ATTEMPT_STATUS_PENDING}
                    ),
                ),
                now=time.time(),
            )

    # LLM: 请求身份已经由宿主鉴权；查找、创建链和唯一事件必须在同一写事务，重复请求不换代。
    # 函数用途: 为显式管理命令登记或复用原 pending 运行，不启动模型或普通任务调度。
    def register_host_command(self, request: HostCommandRequest) -> HostCommandBinding:
        with self.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = read_host_command(conn, request)
            if existing is not None:
                return existing
            return insert_host_command(conn, request, now=time.time())

    # LLM: 只读原请求绑定，查询不能补建链、重开 attempt 或消除 UNKNOWN；联测请求冲突和损坏记录。
    # 函数用途: 供宿主命令查询或重送前取得原始执行身份。
    def find_host_command(self, request: HostCommandRequest) -> HostCommandBinding | None:
        with self._runtime_connection() as conn:
            return read_host_command(conn, request)

    # ------------------------------------------------------------------ Task
    # LLM: Task 是可跨多次执行复用的长期身份，运行终态只能写到 TaskRun。
    # 函数用途: 新建一个 owner 级任务身份，供后续一次或多次 TaskRun 关联。
    def create_task(
        self,
        *,
        owner_id: str,
        task_id: str = "",
        thread_id: str = "",
        conversation_task_id: str = "",
        title: str = "",
        goal: str = "",
    ) -> sqlite3.Row:
        """创建 Task。task_id 缺省由框架铸造（B.1 task_id）。"""
        task_id = task_id or new_id("task_id")
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, owner_id, thread_id, conversation_task_id,
                                  title, goal, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, owner_id, thread_id, conversation_task_id, title, goal, now, now),
            )
            conn.commit()
        row = self.get_task(task_id)
        assert row is not None
        return row

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()

    # -------------------------------------------------------------- TaskRun
    def create_task_run(
        self,
        *,
        task_id: str,
        status: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        task_run_id = new_id("task_run_id")
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (task_run_id, task_id, status, now, now, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            conn.commit()
        row = self.get_task_run(task_run_id)
        assert row is not None
        return row

    def get_task_run(self, task_run_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM task_runs WHERE task_run_id = ?", (task_run_id,)
            ).fetchone()

    def task_runs_for_task(self, task_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return list(rows)

    # LLM: Recovery callers may inspect only still-open TaskRuns; returning canonical rows
    # avoids rescanning historical terminal runs or deriving openness from status aliases.
    # 函数用途: 列出尚未写入 closed_at 的执行总账，供启动/发现层幂等补齐终态。
    def open_task_runs(self, *, limit: int = 0) -> list[sqlite3.Row]:
        normalized_limit = max(0, int(limit or 0))
        query = "SELECT * FROM task_runs WHERE closed_at = 0 ORDER BY created_at"
        params: tuple[object, ...] = ()
        if normalized_limit:
            query += " LIMIT ?"
            params = (normalized_limit,)
        with self._runtime_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return list(rows)

    # ------------------------------------------------------------ AgentRun
    def create_agent_run(
        self,
        *,
        task_run_id: str,
        parent_agent_run_id: str = "",
        role: str = "",
        run_id: str = "",
    ) -> sqlite3.Row:
        """创建 AgentRun。parent 为空 = root 身份（A.5/A.7，合法）。

        root/child 的 current attempt pointer 在 create_attempt 时 CAS 生效。
        """
        agent_run_id = new_id("agent_run_id")
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id,
                                       delegation_id, run_id, role, status,
                                       current_attempt_id, current_attempt_generation,
                                       workspace_epoch, created_at, updated_at)
                VALUES(?, ?, ?, '', ?, ?, '', '', 0, 1, ?, ?)
                """,
                (agent_run_id, task_run_id, parent_agent_run_id, run_id, role, now, now),
            )
            conn.commit()
        row = self.get_agent_run(agent_run_id)
        assert row is not None
        return row

    def get_agent_run(self, agent_run_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM agent_runs WHERE agent_run_id = ?", (agent_run_id,)
            ).fetchone()

    def agent_run_for_run_id(self, run_id: str) -> sqlite3.Row | None:
        """按既有 subagent run_id 找权威 AgentRun（新旧对账键，B.6 重算链路）。"""
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

    # LLM: Reuse the exact current-attempt CAS used by owner-local active-turn recovery;
    # unavailable PID/start identity must leave the row untouched, not guess from age.
    # 函数用途: 启动时统一调和已经证实进程死亡的运行轮，与按用户恢复保持同一判断和写入路径。
    def recover_stale_attempts(self, *, now: float | None = None) -> list[str]:
        """RUN-01（2026-08-15 C1 真机实证）：普通 CLI run 崩溃后悬挂 attempt 的调和。

        现象：CLI run 被 kill -9 后，agent_runs 卡 created、attempt 卡 running，
        startup_recovery 的崩溃检测只覆盖子代理后台任务（无 pid 记录），导致
        悬挂 attempt 永久不可见。本函数用 attempt 落账的 runner 身份做进程死亡
        证明：仅当 pid 明确查无此进程（ProcessLookupError）或 pid 存活但
        start_time 不匹配（pid 复用 = 原进程已死）才归 unknown；无 pid 记录、
        进程存活且 start_time 匹配、或不可证实（unverifiable）一律保持原态
        （fail-closed 不猜）。判定与 scheduler P0-4 recover_interrupted_executions
        及 长期助手 cron/executions.py 同模式；只调和 current_attempt 非终态的 run。

        函数用途: 把崩溃且进程死亡被证实的悬挂 run/attempt 收敛为 unknown 终态，
        返回被调和（归 unknown）的 agent_run_id 列表。
        """
        from .operations import AGENT_RUN_TERMINAL_STATUSES as _TERMINAL

        current = time.time() if now is None else now
        recovered: list[str] = []
        with self._runtime_connection() as conn:
            rows = conn.execute(
                """
                SELECT ar.agent_run_id, ar.current_attempt_id,
                       at.metadata_json, at.status AS attempt_status
                FROM agent_runs ar
                JOIN agent_attempts at ON at.attempt_id = ar.current_attempt_id
                WHERE at.status IN ('running', 'created')
                """,
            ).fetchall()
            for row in rows:
                if _mark_dead_runner_attempt_unknown(conn, row, current):
                    recovered.append(str(row["agent_run_id"] or ""))
            conn.commit()
        return recovered

    # LLM: This is the only automatic exception to the generic UNKNOWN hard stop.  It accepts
    # two exact durable identities plus a complete operation-record projection, then proves in
    # one transaction that no handler outcome or mutation is uncertain.  Never broaden it to a
    # task-only match, model text, error strings, PID age, or a best-effort archive scan.
    # Owner Agents may be loaded lazily after Gateway startup: reconcile only this exact
    # current runner using durable PID/start identity, then apply the unchanged effect proof.
    # 函数用途: 按用户和精确执行绑定调和已死进程，再核对工具账；不要求默认管理员先代办所有用户恢复。
    def recover_recorded_active_turn_attempt(
        self,
        *,
        task_id: str,
        run_id: str,
        recorded_operation_facts: dict[str, dict[str, str]],
        operator: str,
        expected_attempt_id: str = "",
        expected_agent_run_id: str = "",
    ) -> dict[str, Any]:
        """Recover one exact root turn only when every started effect is durable.

        Generic ``unknown`` attempts still require ``recover_attempt_unknown``.
        This narrower path is authorized only by an already validated active-turn
        recovery marker at the transport layer. RuntimeDB independently requires
        the exact task+run pair, the current unknown (or provably dead) attempt, terminal tool rows,
        matching durable operation records, and no MUTATING/DIRTY resource.
        """

        selected_task_id = str(task_id or "").strip()
        selected_run_id = str(run_id or "").strip()
        if not selected_run_id:
            return {"recovered": False, "status": "blocked", "reason": "identity_missing"}
        recorded = _normalize_recorded_operation_facts(recorded_operation_facts)
        with self.transaction() as conn:
            if not selected_task_id:
                exists = conn.execute(
                    "SELECT 1 FROM agent_runs WHERE run_id = ? LIMIT 1",
                    (selected_run_id,),
                ).fetchone()
                return {
                    "recovered": False,
                    "status": "blocked" if exists is not None else "absent",
                    "reason": "identity_missing" if exists is not None else "no_exact_run",
                }
            row = _exact_active_turn_run(conn, selected_task_id, selected_run_id)
            if row is None:
                return {"recovered": False, "status": "absent", "reason": "no_exact_run"}
            attempt_id = str(row["current_attempt_id"] or "")
            if ((expected_attempt_id and attempt_id != expected_attempt_id)
                    or (expected_agent_run_id and row["agent_run_id"] != expected_agent_run_id)):
                return {"recovered": False, "status": "blocked", "reason": "execution_binding_changed"}
            attempt_status = str(row["attempt_status"] or "")
            if attempt_status in {"running", "created"} and _mark_dead_runner_attempt_unknown(
                conn, row, time.time(),
            ):
                attempt_status = ATTEMPT_STATUS_UNKNOWN
            recovery_reason = _main_agent_recovery_reason(
                str(row["run_status"] or ""),
                attempt_status,
            )
            if attempt_status != ATTEMPT_STATUS_UNKNOWN:
                not_required = not recovery_reason and attempt_status in _ATTEMPT_TERMINAL_STATUSES
                return {
                    "recovered": False,
                    "status": "not_required" if not_required else "blocked",
                    "reason": "attempt_already_terminal" if not_required else (
                        recovery_reason or "attempt_not_unknown"
                    ),
                    "attempt_id": attempt_id,
                }
            operation_report = _active_turn_operation_recovery_report(
                conn,
                attempt_id,
                recorded,
            )
            if operation_report.get("ok") is not True:
                return {
                    "recovered": False,
                    "status": "blocked",
                    "attempt_id": attempt_id,
                    **operation_report,
                }
            mutation_ids = _uncertain_attempt_mutation_ids(conn, attempt_id)
            if mutation_ids:
                return {
                    "recovered": False,
                    "status": "blocked",
                    "reason": "resource_state_uncertain",
                    "attempt_id": attempt_id,
                    "blocking_mutation_ids": mutation_ids,
                }
            _cancel_unstarted_tool_operations(
                conn,
                agent_run_id=str(row["agent_run_id"] or ""),
                attempt_id=attempt_id,
                now=time.time(),
            )
            recorded_count = int(operation_report["recorded_operation_count"] or 0)
            result = _recover_unknown_attempt_conn(
                self,
                conn,
                attempt_id=attempt_id,
                operator=operator,
                effect_disposition="recorded" if recorded_count else "confirmed_noop",
                reason="同一 active turn 崩溃重排；已核对全部工具终态与耐久记录",
                event_facts={
                    "recovery_mode": "recorded_active_turn",
                    "task_id": selected_task_id,
                    "run_id": selected_run_id,
                    **operation_report,
                },
            )
            return {**result, "status": "recovered" if result.get("recovered") else "blocked"}

    # LLM: This upgrade repair trusts only the current_attempt pointer, never PID age or model
    # prose. A non-current pending/running row has no execution authority and must use the same
    # UNKNOWN/DIRTY cleanup as live takeover. Owner supervision calls this as an idempotent no-op.
    # 函数用途: 清理旧版本遗留的非 current 幽灵运行轮，返回实际收口的 attempt ID。
    def reconcile_superseded_attempts(self, *, now: float | None = None) -> list[str]:
        current = time.time() if now is None else now
        reconciled: list[str] = []
        with self.transaction() as conn:
            runs = conn.execute(
                "SELECT DISTINCT ar.agent_run_id, ar.current_attempt_id, "
                "ar.current_attempt_generation, ar.task_run_id "
                "FROM agent_runs ar JOIN agent_attempts aa "
                "ON aa.agent_run_id = ar.agent_run_id "
                "WHERE ar.current_attempt_id != '' "
                "AND aa.attempt_id != ar.current_attempt_id "
                "AND aa.status IN (?, 'running') AND aa.ended_at = 0",
                (ATTEMPT_STATUS_PENDING,),
            ).fetchall()
            for run in runs:
                reconciled.extend(
                    self._supersede_noncurrent_attempts_conn(
                        conn,
                        agent_run_id=str(run["agent_run_id"] or ""),
                        current_attempt_id=str(run["current_attempt_id"] or ""),
                        current_generation=int(
                            run["current_attempt_generation"] or 0
                        ),
                        task_run_id=str(run["task_run_id"] or ""),
                        now=current,
                        reason="stale_noncurrent_reconcile",
                    )
                )
        return reconciled

    def task_id_for_run_id(self, run_id: str) -> str:
        """授权门同款 JOIN：run_id → 权威链 task_id（runner 身份回填用）。"""
        with self._runtime_connection() as conn:
            row = conn.execute(
                """
                SELECT tr.task_id
                FROM agent_runs ar
                JOIN task_runs tr ON tr.task_run_id = ar.task_run_id
                WHERE ar.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return str(row["task_id"]) if row is not None else ""

    def agent_runs_for_task_run(self, task_run_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE task_run_id = ? ORDER BY created_at",
                (task_run_id,),
            ).fetchall()
        return list(rows)

    def main_agent_run_for_task(self, task_id: str) -> sqlite3.Row | None:
        """task 的 role='main' root AgentRun（终态/锁过滤的权威锚点）。

        tasks → task_runs → agent_runs（role='main' 且 parent 空 = 主链根），
        取最新一条。R1-03 补漏：主代理续跑身份（bg-main-thread-{thread}）与
        gateway 请求身份（req_{id}）不同，按 run_id 查不到对方登记——回退
        按 task 查主链 run，续跑必须复用原 run（create_attempt 轮换
        generation），禁止同一 task 分裂第二棵 run 树（真机实证：unfinished
        任务续跑分裂出 taskrun 双份，挂载闸被绕过）。无权威记录 → None。
        """
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT ar.* FROM agent_runs ar "
                "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id "
                "JOIN tasks t ON t.task_id = tr.task_id "
                "WHERE t.task_id = ? AND ar.role = 'main' AND ar.parent_agent_run_id = '' "
                "ORDER BY ar.created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()

    # LLM: 后台调度只能读取此结构化投影决定“保留事件但不自动挂载”；最终执行权
    # 仍由 create_attempt 的同源事务闸裁决，不能把本方法当作执行许可。
    # 函数用途: 查询某任务主代理是否因 unknown 状态必须等待显式人工恢复。
    def main_agent_recovery_block_for_task(self, task_id: str) -> dict[str, str] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT ar.agent_run_id, ar.status AS run_status, "
                "ar.current_attempt_id, aa.status AS attempt_status "
                "FROM agent_runs ar "
                "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id "
                "JOIN tasks t ON t.task_id = tr.task_id "
                "LEFT JOIN agent_attempts aa ON aa.attempt_id = ar.current_attempt_id "
                "WHERE t.task_id = ? AND ar.role = 'main' "
                "AND ar.parent_agent_run_id = '' "
                "ORDER BY ar.created_at DESC LIMIT 1",
                (str(task_id or ""),),
            ).fetchone()
        if row is None:
            return None
        reason = _main_agent_recovery_reason(
            str(row["run_status"] or ""),
            str(row["attempt_status"] or ""),
        )
        if not reason:
            return None
        return {
            "schema_version": "main-agent-recovery-block.v1",
            "reason": reason,
            "task_id": str(task_id or ""),
            "agent_run_id": str(row["agent_run_id"] or ""),
            "attempt_id": str(row["current_attempt_id"] or ""),
            "run_status": str(row["run_status"] or ""),
            "attempt_status": str(row["attempt_status"] or ""),
        }

    # LLM: Subagent orphan recovery must consult this projection before it
    # schedules a replacement worker. The transactional create_attempt gate
    # remains the final authority; this read-side check prevents known-invalid
    # starts from being reported as revived or consuming a runner slot.
    # 函数用途: 按子代理 run_id 查询 unknown 权威状态，供自动孤儿恢复在启动前停手。
    def agent_run_recovery_block_for_run_id(self, run_id: str) -> dict[str, str] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT ar.agent_run_id, ar.status AS run_status, "
                "ar.current_attempt_id, aa.status AS attempt_status "
                "FROM agent_runs ar "
                "LEFT JOIN agent_attempts aa ON aa.attempt_id = ar.current_attempt_id "
                "WHERE ar.run_id = ? LIMIT 1",
                (str(run_id or ""),),
            ).fetchone()
        if row is None:
            return None
        reason = _main_agent_recovery_reason(
            str(row["run_status"] or ""),
            str(row["attempt_status"] or ""),
        )
        if not reason:
            return None
        return {
            "schema_version": "agent-run-recovery-block.v1",
            "reason": reason,
            "run_id": str(run_id or ""),
            "agent_run_id": str(row["agent_run_id"] or ""),
            "attempt_id": str(row["current_attempt_id"] or ""),
            "run_status": str(row["run_status"] or ""),
            "attempt_status": str(row["attempt_status"] or ""),
        }

    # ---------------------------------------------------------- AgentAttempt
    # LLM: A delegated child is registered as pending before any worker owns it.
    # Activation must reuse that exact current generation, install the execution
    # lock, and replace registration metadata with the real runner identity.
    # 函数用途: 在同一事务里把已登记但未启动的子代理 attempt 激活为真实运行态。
    def _activate_pending_attempt_conn(
        self,
        conn: sqlite3.Connection,
        *,
        run: sqlite3.Row,
        attempt: sqlite3.Row,
        now: float,
        scope: str,
    ) -> sqlite3.Row:
        attempt_id = str(attempt["attempt_id"] or "")
        agent_run_id = str(attempt["agent_run_id"] or "")
        generation = int(attempt["attempt_generation"] or 0)
        lock = conn.execute(
            "SELECT * FROM resource_locks WHERE canonical_scope = ?", (scope,)
        ).fetchone()
        if lock is not None:
            raise RuntimeConflictError(
                f"pending attempt 已存在执行权锁，拒绝重复启动: {scope}"
            )
        metadata_json = json.dumps(_runner_identity_metadata(), ensure_ascii=False)
        updated_attempt = conn.execute(
            "UPDATE agent_attempts SET status = 'running', started_at = ?, metadata_json = ? "
            "WHERE attempt_id = ? AND agent_run_id = ? AND status = ? AND ended_at = 0",
            (
                now,
                metadata_json,
                attempt_id,
                agent_run_id,
                ATTEMPT_STATUS_PENDING,
            ),
        ).rowcount
        if updated_attempt != 1:
            raise RuntimeConflictError(
                f"pending attempt 激活 CAS 失败: {agent_run_id} generation {generation}"
            )
        updated_run = conn.execute(
            "UPDATE agent_runs SET status = 'created', updated_at = ? "
            "WHERE agent_run_id = ? AND current_attempt_id = ? "
            "AND current_attempt_generation = ? AND status IN ('', 'created')",
            (now, agent_run_id, attempt_id, generation),
        ).rowcount
        if updated_run != 1:
            raise RuntimeConflictError(
                f"pending AgentRun 激活 CAS 失败: {agent_run_id} generation {generation}"
            )
        conn.execute(
            """
            INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance,
                                       pid, start_token, attempt_id,
                                       attempt_generation, workspace_epoch,
                                       tool_operation_generation, lease_expires_at,
                                       created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                scope,
                self.instance_id,
                os.getpid(),
                self._start_token(),
                attempt_id,
                generation,
                int(run["workspace_epoch"] or 1),
                now + EXEC_LOCK_LEASE_SECONDS,
                now,
                now,
            ),
        )
        for event_type in ("agent_attempt.started", "agent_run.started"):
            self._append_event_conn(
                conn,
                event_type=event_type,
                attempt_id=attempt_id,
                agent_run_id=agent_run_id,
                task_run_id=str(run["task_run_id"] or ""),
                payload={
                    "previous_status": ATTEMPT_STATUS_PENDING,
                    "status": "running",
                    "attempt_generation": generation,
                },
            )
        row = conn.execute(
            "SELECT * FROM agent_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        assert row is not None
        return row

    # LLM: current pointer 仍是唯一执行代次权威；正常续轮、活动接管和升级调和共用本事务。
    # 只将未确认资源保守置 DIRTY，保留 STABLE 和既有 DIRTY 证据；同步检查换代与 mutation 回归。
    # 函数用途: 收口被替代的旧执行轮并释放其锁；已确认资源保持可用，未完成副作用继续阻断。
    def _supersede_noncurrent_attempts_conn(
        self,
        conn: sqlite3.Connection,
        *,
        agent_run_id: str,
        current_attempt_id: str,
        current_generation: int,
        task_run_id: str,
        now: float,
        reason: str,
        extra_cleanup_attempt_ids: tuple[str, ...] = (),
    ) -> list[str]:
        stale_attempts = conn.execute(
            "SELECT attempt_id, attempt_generation, status FROM agent_attempts "
            "WHERE agent_run_id = ? AND attempt_id != ? "
            "AND status IN (?, 'running') AND ended_at = 0 "
            "ORDER BY attempt_generation ASC",
            (agent_run_id, current_attempt_id, ATTEMPT_STATUS_PENDING),
        ).fetchall()
        superseded_ids: list[str] = []
        for stale_attempt in stale_attempts:
            stale_attempt_id = str(stale_attempt["attempt_id"] or "")
            updated_stale = conn.execute(
                "UPDATE agent_attempts SET status = 'cancelled', ended_at = ? "
                "WHERE attempt_id = ? AND status IN (?, 'running') AND ended_at = 0",
                (now, stale_attempt_id, ATTEMPT_STATUS_PENDING),
            ).rowcount
            if updated_stale != 1:
                raise RuntimeConflictError(
                    f"旧 attempt 收口 CAS 失败: {stale_attempt_id}"
                )
            superseded_ids.append(stale_attempt_id)
            self._append_event_conn(
                conn,
                event_type="agent_attempt.superseded",
                attempt_id=stale_attempt_id,
                agent_run_id=agent_run_id,
                task_run_id=task_run_id,
                payload={
                    "previous_status": str(stale_attempt["status"] or ""),
                    "status": "cancelled",
                    "reason": reason,
                    "superseded_by_attempt_id": current_attempt_id,
                    "superseded_by_generation": current_generation,
                },
            )

        cleanup_ids = list(superseded_ids)
        for extra_attempt_id in extra_cleanup_attempt_ids:
            normalized = str(extra_attempt_id or "").strip()
            if normalized and normalized not in cleanup_ids:
                cleanup_ids.append(normalized)
        for cleanup_attempt_id in cleanup_ids:
            # outcome_json 保留 holder/lease/resource_scopes 等 claim 元数据；
            # 只追加结构化未知原因，不能整段覆盖后丢掉副作用追踪锚点。
            stale_rows = conn.execute(
                "SELECT operation_id, outcome_json FROM tool_operations "
                "WHERE attempt_id = ? AND status IN ('CLAIMED', 'EXECUTING')",
                (cleanup_attempt_id,),
            ).fetchall()
            for stale in stale_rows:
                payload = {}
                try:
                    loaded = json.loads(stale["outcome_json"])
                except (TypeError, json.JSONDecodeError):
                    loaded = {}
                if isinstance(loaded, dict):
                    payload = dict(loaded)
                payload["reason"] = reason
                payload["unknown_reason"] = reason
                conn.execute(
                    "UPDATE tool_operations SET status = 'UNKNOWN', outcome_json = ?, "
                    "updated_at = ? WHERE operation_id = ? "
                    "AND status IN ('CLAIMED', 'EXECUTING')",
                    (
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        stale["operation_id"],
                    ),
                )
            # 非 current attempt 已永久失去执行权；UNKNOWN 是结果不可知终态，
            # 旧锁必须释放；STABLE 已有确认事实，不能因换代重新判为未知。
            # 其余未确认 mutation 保守标 DIRTY，已有 DIRTY 的原因和时间保持。
            conn.execute(
                "DELETE FROM resource_locks WHERE attempt_id = ?",
                (cleanup_attempt_id,),
            )
            conn.execute(
                "UPDATE resource_mutations SET state = 'DIRTY', dirty_reason = ?, "
                "updated_at = ? WHERE attempt_id = ? AND state NOT IN ('STABLE', 'DIRTY')",
                (reason, now, cleanup_attempt_id),
            )
        return superseded_ids

    # LLM: An explicit external input may reserve the next turn for one existing
    # nonterminal logical agent while no runner owns execution. This method only
    # creates/reuses a pending attempt; create_attempt(reuse_pending=True) remains
    # the sole activation edge that acquires the execution lock. Callers must
    # still enforce task lifecycle, owner authorization, and terminal read-only
    # rules before using this repository primitive.
    # expected_current_attempt_id/pending_attempt_id 成对固定外层已经锁定的旧、新轮；事务不得换领后来 current。
    # 函数用途: 给暂时空闲或等待孩子的同一个代理预留下一执行轮；这里只排队，不启动模型或工具。
    def queue_pending_attempt(
        self,
        agent_run_id: str,
        *,
        source: str = "external_input",
        expected_current_attempt_id: str | None = None,
        pending_attempt_id: str | None = None,
    ) -> sqlite3.Row:
        """Create or reuse one unstarted current attempt for an explicit wake."""

        now = time.time()
        normalized_source = str(source or "external_input").strip() or "external_input"
        exact = expected_current_attempt_id is not None or pending_attempt_id is not None
        if exact and (
            not str(expected_current_attempt_id or "").strip()
            or not str(pending_attempt_id or "").strip()
            or expected_current_attempt_id == pending_attempt_id
        ):
            raise ValueError("准确预留必须同时提供不同的原轮和新轮身份")
        scope = exec_lock_scope(agent_run_id)
        with self.transaction() as conn:
            # Reserve the writer before reading current_generation. Two TUI/Web
            # submissions may target the same idle child concurrently; the
            # second caller must observe and reuse the first pending row rather
            # than race the UNIQUE(agent_run_id, generation) constraint.
            conn.execute("BEGIN IMMEDIATE")
            run, current = self._load_pending_attempt_source_conn(
                conn,
                agent_run_id,
            )
            if exact and str(current["attempt_id"]) != expected_current_attempt_id:
                raise RuntimeConflictError("预留原轮已变化，不能复用后来的 current")
            reusable = self._validate_pending_attempt_transition_conn(
                conn,
                run=run,
                current=current,
                agent_run_id=agent_run_id,
                scope=scope,
                source=normalized_source,
            )
            if reusable is not None:
                if exact:
                    raise RuntimeConflictError("准确后继预留不能替换或复用尚未启动的原轮")
                return reusable
            return self._insert_pending_attempt_conn(
                conn,
                run=run,
                current=current,
                agent_run_id=agent_run_id,
                source=normalized_source,
                now=now,
                attempt_id=pending_attempt_id or new_id("attempt_id"),
            )

    # LLM: The writer transaction must read the run pointer and pointed attempt
    # together. Missing pointers are corruption/conflict, never an empty agent.
    # 函数用途: 在排队事务中读取同一 AgentRun 的 current pointer 和执行轮。
    def _load_pending_attempt_source_conn(
        self,
        conn: sqlite3.Connection,
        agent_run_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        run = conn.execute(
            "SELECT current_attempt_generation, current_attempt_id, "
            "status, task_run_id, workspace_epoch FROM agent_runs "
            "WHERE agent_run_id = ?",
            (agent_run_id,),
        ).fetchone()
        if run is None:
            raise KeyError(f"agent_run 不存在: {agent_run_id}")
        current_attempt_id = str(run["current_attempt_id"] or "")
        current = conn.execute(
            "SELECT attempt_id, agent_run_id, attempt_generation, status, ended_at "
            "FROM agent_attempts WHERE attempt_id = ? AND agent_run_id = ?",
            (current_attempt_id, agent_run_id),
        ).fetchone()
        if current is None:
            raise RuntimeConflictError(
                f"current attempt 缺失，拒绝排队新轮次: {agent_run_id}"
            )
        return run, current

    # LLM: Reuse is allowed only for one unstarted current pending row. Running,
    # unknown, nonterminal or still-locked predecessors fail closed before any
    # generation is inserted.
    # 函数用途: 校验旧执行轮是否允许续接；已有 pending 直接复用，其它冲突明确拒绝。
    def _validate_pending_attempt_transition_conn(
        self,
        conn: sqlite3.Connection,
        *,
        run: sqlite3.Row,
        current: sqlite3.Row,
        agent_run_id: str,
        scope: str,
        source: str,
    ) -> sqlite3.Row | None:
        current_status = str(current["status"] or "").strip().lower()
        current_ended_at = float(current["ended_at"] or 0.0)
        if current_status == ATTEMPT_STATUS_PENDING and current_ended_at == 0:
            return current
        if current_status == "running" and current_ended_at == 0:
            raise RuntimeExecutionBusyError(
                f"current attempt 仍在运行，拒绝另排轮次: {agent_run_id}"
            )
        recovery_reason = _main_agent_recovery_reason(
            str(run["status"] or ""),
            current_status,
        )
        if recovery_reason:
            self._append_event_conn(
                conn,
                event_type="agent_attempt.queue_blocked",
                attempt_id=str(current["attempt_id"] or ""),
                agent_run_id=agent_run_id,
                task_run_id=str(run["task_run_id"] or ""),
                payload={"reason": recovery_reason, "source": source},
            )
            raise RuntimeConflictError(
                f"attempt 不可安全续接({recovery_reason})，拒绝排队: {agent_run_id}"
            )
        if current_status not in _ATTEMPT_TERMINAL_STATUSES:
            raise RuntimeConflictError(
                f"attempt 状态不可续接({current_status!r})，拒绝排队: {agent_run_id}"
            )
        lock = conn.execute(
            "SELECT attempt_id FROM resource_locks WHERE canonical_scope = ?",
            (scope,),
        ).fetchone()
        if lock is not None:
            raise RuntimeExecutionBusyError(
                f"执行权锁仍存在，拒绝排队新轮次: {scope}"
            )
        return None

    # LLM: Insertion and current-pointer CAS share the same immediate writer
    # transaction. Events describe the committed generation and never trigger it.
    # attempt_id 由原统一生成器铸造，可在外层锁定新旧回合前分配；本事务才使其成为执行账事实。
    # 函数用途: 原子插入下一 pending attempt、切换 current pointer 并记录排队事件。
    def _insert_pending_attempt_conn(
        self,
        conn: sqlite3.Connection,
        *,
        run: sqlite3.Row,
        current: sqlite3.Row,
        agent_run_id: str,
        source: str,
        now: float,
        attempt_id: str,
    ) -> sqlite3.Row:
        current_attempt_id = str(current["attempt_id"] or "")
        current_status = str(current["status"] or "").strip().lower()
        previous_generation = int(run["current_attempt_generation"] or 0)
        generation = previous_generation + 1
        conn.execute(
            """
            INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation,
                                       status, started_at, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                agent_run_id,
                generation,
                ATTEMPT_STATUS_PENDING,
                now,
                json.dumps(
                    {"lifecycle": ATTEMPT_STATUS_PENDING, "source": source},
                    ensure_ascii=False,
                ),
            ),
        )
        updated = conn.execute(
            """
            UPDATE agent_runs
            SET current_attempt_id = ?, current_attempt_generation = ?,
                status = 'created', updated_at = ?
            WHERE agent_run_id = ? AND current_attempt_id = ?
              AND current_attempt_generation = ? AND status = ?
            """,
            (
                attempt_id,
                generation,
                now,
                agent_run_id,
                current_attempt_id,
                previous_generation,
                str(run["status"] or ""),
            ),
        ).rowcount
        if updated != 1:
            raise RuntimeConflictError(
                f"pending attempt 排队 CAS 失败: {agent_run_id} "
                f"generation {previous_generation}->{generation}"
            )
        for event_type in ("agent_attempt.queued", "agent_run.queued"):
            self._append_event_conn(
                conn,
                event_type=event_type,
                attempt_id=attempt_id,
                agent_run_id=agent_run_id,
                task_run_id=str(run["task_run_id"] or ""),
                payload={
                    "previous_attempt_id": current_attempt_id,
                    "previous_status": current_status,
                    "status": ATTEMPT_STATUS_PENDING,
                    "attempt_generation": generation,
                    "source": source,
                },
            )
        row = conn.execute(
            "SELECT * FROM agent_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert row is not None
        return row

    # LLM: 创建 attempt 的事务闸与后台只读 recovery 投影必须共用
    # _main_agent_recovery_reason；只读 preflight 不能替代这里的最终 CAS。
    # 换代必须同时关闭所有非 current 的 active attempt，但不能把旧工具副作用
    # 猜成成功：未决工具仍转 UNKNOWN、未确认 mutation 仍转 DIRTY，已确认 STABLE 保留。
    # 合法恢复若复用已关闭 TaskRun，须在同一事务重开总账并留事件；不允许运行中的树仍展示旧 cancelled。
    # expected_pending_attempt_id 是宿主排队时冻结的身份；传入后只能激活该 pending，不能换领 current 或创建后继。
    # 函数用途: 原子取得新执行权、收掉旧运行态并同步整棵执行总账；准入拒绝时这些状态均不改变。
    def create_attempt(
        self,
        agent_run_id: str,
        *,
        reuse_pending: bool = False,
        reject_running: bool = False,
        expected_pending_attempt_id: str | None = None,
    ) -> sqlite3.Row:
        """单事务创建新 attempt + 原子取得执行权（R1-03 v5）。

        挂载闸（防线分层）：自喂循环（settle 置 done 后仍被每 cooldown 挂新
        running attempt，真机 agentrun-1786466638 被驱动 13 次实证）的根治在
        发现层——owner_wake_discovery 终态过滤（终态 run 一律不催，不调本函数）
        + 本函数执行权锁（活跃锁拒绝并发双挂载）。终态（done/failed/cancelled）
        挂载本身是合法显式调度：子代理 followup 打回重跑（capability grant/
        写授权后同 run 重挂）、policy due 周期轮换（run_id 稳定派生
        bg-main-{thread_id}，成功轮 settle done 后下次 due 仍同 run 重挂）、
        user-stop 恢复。任务级生命周期闸（子代理 _assert_runner_attempt_
        start_allowed / policy 只对 active 任务调度）才是「该不该重跑」的裁决
        者，run 级终态不拦截。未知状态（含历史 unfinished 遗留非法值）→
        fail-closed 拒绝 + status_conflict 诊断事件（不写 settled/completed）。
        '' 按 created 兼容映射（迁移红线）。

        执行权锁三态（scope=attempt-exec:{agent_run_id}，与 CAS 换代同事务）：
          - 缺失 → INSERT 锁（归属新 attempt）；
          - 本实例持有 → 换代（compact/账本续跑轮 = 同一 worker 续跑）；
          - 他人持有且进程存活 → RuntimeExecutionBusyError（并发双挂载恰一成功）；
          - PID/start token 确认持主死亡 → 立即接管（删旧锁建新锁）；
          - 无法确认持主死亡 → fail-closed 保留旧锁。
        锁释放四元组（scope+holder_instance+attempt_id+attempt_generation），
        settle 终态即释放；takeover/接管同事务删旧锁。

        CAS 换代（F.2/F.3）：0 行命中 = 并发冲突，fail-closed；旧 attempt
        的 ID/generation/执行证据不改写，但生命周期原子转 cancelled，追加
        agent_attempt.superseded 事件。历史版本遗留的其他非 current active
        attempt 也在同一事务收口，避免幽灵 RUNNING 累积。G4 takeover
        （G4-4）继续把旧 attempt 非终态 operation 统一转 UNKNOWN、未确认
        mutation 标 DIRTY，已确认 STABLE 和既有 DIRTY 证据保留；旧 worker
        即使还活着也由 current fence 拒绝，不能因资源稳定而继续执行。

        ``reuse_pending`` 只给真实 runner 启动路径使用：如果 current attempt
        仍是 pending，就原子激活 generation 1；``reject_running`` 同时阻止
        同一进程里的重复 dispatcher 偷偷轮换成 generation 2。
        """
        now = time.time()
        scope = exec_lock_scope(agent_run_id)
        with self._runtime_connection() as conn:
            if expected_pending_attempt_id is not None:
                if not expected_pending_attempt_id or not reuse_pending:
                    raise ValueError("精确激活必须提供非空 pending 身份并启用 reuse_pending")
                conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT current_attempt_generation, current_attempt_id, "
                "status, task_run_id, workspace_epoch FROM agent_runs "
                "WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"agent_run 不存在: {agent_run_id}")
            status = str(run["status"] or "")
            latest_attempt = conn.execute(
                "SELECT attempt_id, agent_run_id, attempt_generation, status, ended_at "
                "FROM agent_attempts WHERE agent_run_id = ? "
                "ORDER BY attempt_generation DESC LIMIT 1",
                (agent_run_id,),
            ).fetchone()
            latest_attempt_status = (
                str(latest_attempt["status"] or "") if latest_attempt is not None else ""
            )
            if expected_pending_attempt_id is not None and (
                latest_attempt is None
                or str(run["current_attempt_id"] or "") != expected_pending_attempt_id
                or str(latest_attempt["attempt_id"] or "") != expected_pending_attempt_id
                or latest_attempt_status != ATTEMPT_STATUS_PENDING
                or float(latest_attempt["ended_at"] or 0) != 0
            ):
                raise RuntimeConflictError("预留执行轮已失效，拒绝重新领取执行权")
            if (
                reuse_pending
                and latest_attempt is not None
                and str(latest_attempt["attempt_id"] or "")
                == str(run["current_attempt_id"] or "")
                and latest_attempt_status == ATTEMPT_STATUS_PENDING
                and float(latest_attempt["ended_at"] or 0) == 0
            ):
                activated = self._activate_pending_attempt_conn(
                    conn,
                    run=run,
                    attempt=latest_attempt,
                    now=now,
                    scope=scope,
                )
                conn.commit()
                return activated
            if (
                reject_running
                and latest_attempt is not None
                and str(latest_attempt["attempt_id"] or "")
                == str(run["current_attempt_id"] or "")
                and latest_attempt_status == "running"
                and float(latest_attempt["ended_at"] or 0) == 0
            ):
                raise RuntimeConflictError(
                    f"current attempt 仍在运行，拒绝重复启动: {agent_run_id}"
                )
            recovery_reason = _main_agent_recovery_reason(status, latest_attempt_status)
            # 终态（done/failed/cancelled）挂载放行：见 docstring 防线分层——
            # 自喂防护在发现层过滤 + 执行权锁，任务级生命周期闸裁决「该不该重跑」。
            # 合法状态 = 现役（''/created）+ 全部终态；其余（历史 unfinished 等
            # 非法值）→ fail-closed。
            if recovery_reason == "unknown_run_status":
                self._append_event_conn(
                    conn,
                    event_type="status_conflict",
                    attempt_id=str(run["current_attempt_id"] or ""),
                    agent_run_id=agent_run_id,
                    task_run_id=str(run["task_run_id"] or ""),
                    payload={"status": status, "action": "create_attempt_blocked",
                             "reason": "unknown_run_status"},
                )
                # 诊断事件先落库再拒绝：raise 会让 with 事务回滚，事件必须
                # 先行 commit（拒绝路径本身不产生其他写，回滚空事务无害）。
                conn.commit()
                raise RuntimeConflictError(
                    f"run 状态未知({status!r})，fail-closed 拒绝挂载: {agent_run_id}"
                )
            # attempt 层 unknown 闸（2026-08-15 双席核对点3）：最新 attempt
            # 标 unknown（执行者死亡+结果未知）→ 自动挂载被拒——不能触发
            # 自动续跑；recover_attempt_unknown 人工核对后（→ recovered）
            # 才放行。锁保留在 _mark_attempt_unknown 时没有释放，即使此处
            # 误过闸，takeover 也不应发生——本闸是结构化第一道。
            if recovery_reason == "attempt_unknown_terminal":
                self._append_event_conn(
                    conn,
                    event_type="attempt_unknown_blocked",
                    attempt_id=str(latest_attempt["attempt_id"] or ""),
                    agent_run_id=agent_run_id,
                    task_run_id=str(run["task_run_id"] or ""),
                    payload={"status": ATTEMPT_STATUS_UNKNOWN,
                             "action": "create_attempt_blocked",
                             "reason": "attempt_unknown_terminal",
                             "hint": "执行者死亡且结果不可知, 自动拉起被拒; "
                                     "人工核对后 recover_attempt_unknown 显式恢复"},
                )
                conn.commit()
                raise RuntimeConflictError(
                    f"attempt 未知终态(unknown)，fail-closed 拒绝挂载: {agent_run_id}"
                )
            lock = conn.execute(
                "SELECT * FROM resource_locks WHERE canonical_scope = ?", (scope,)
            ).fetchone()
            if lock is not None:
                holder = str(lock["holder_instance"] or "")
                if holder != self.instance_id and not self._lock_is_takeoverable(lock, now):
                    raise RuntimeExecutionBusyError(
                        f"执行权锁被 {holder} 持有（活跃），拒绝并发挂载: {scope}"
                    )
                # 接管：删旧锁（同事务），新 attempt 成为唯一持锁者
                conn.execute(
                    "DELETE FROM resource_locks WHERE canonical_scope = ?", (scope,)
                )
            generation = int(run["current_attempt_generation"]) + 1
            old_attempt_id = str(run["current_attempt_id"] or "")
            attempt_id = new_id("attempt_id")
            # 账链关联(2026-08-15 双席边界①): 最新 attempt 为 recovered(人工
            # 核对后显式恢复)时, 新 attempt metadata 记 recovered_from_attempt_id
            # ——recovered → 新 attempt 的关联可追溯, 且可对照新 attempt 的
            # tool ledger 确认未重放原 UNKNOWN 工具。
            recovered_from = ""
            if latest_attempt is not None and \
                    str(latest_attempt["status"] or "") == ATTEMPT_STATUS_RECOVERED:
                recovered_from = str(latest_attempt["attempt_id"] or "")
            meta_json = json.dumps(_runner_identity_metadata(), ensure_ascii=False)
            if recovered_from:
                meta = _runner_identity_metadata()
                meta["recovered_from_attempt_id"] = recovered_from
                meta_json = json.dumps(meta, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation,
                                           status, started_at, metadata_json)
                VALUES(?, ?, ?, 'running', ?, ?)
                """,
                (attempt_id, agent_run_id, generation, now, meta_json),
            )
            conn.execute(
                """
                INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance,
                                           pid, start_token, attempt_id,
                                           attempt_generation, workspace_epoch,
                                           tool_operation_generation, lease_expires_at,
                                           created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (uuid.uuid4().hex, scope, self.instance_id, os.getpid(),
                 self._start_token(), attempt_id, generation,
                 int(run["workspace_epoch"] or 1) if "workspace_epoch" in run.keys()
                 else 1,
                 now + EXEC_LOCK_LEASE_SECONDS, now, now),
            )
            updated = conn.execute(
                """
                UPDATE agent_runs
                SET current_attempt_id = ?, current_attempt_generation = ?,
                    status = 'created', updated_at = ?
                WHERE agent_run_id = ? AND current_attempt_generation = ? AND status = ?
                """,
                (attempt_id, generation, now, agent_run_id, generation - 1, status),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"attempt CAS 失败: {agent_run_id} generation {generation - 1}->{generation}"
                )
            self._reopen_task_run_for_attempt_conn(conn, run, agent_run_id, attempt_id, now)
            self._append_event_conn(
                conn,
                event_type="agent_run.started",
                attempt_id=attempt_id,
                agent_run_id=agent_run_id,
                task_run_id=str(run["task_run_id"] or ""),
                payload={
                    "previous_status": status,
                    "status": "created",
                    "attempt_generation": generation,
                },
            )
            # current pointer 已成功换代：统一收掉旧 active attempt。正常只有
            # old_attempt_id；全量查询同时修复升级前遗留的幽灵 RUNNING/PENDING。
            self._supersede_noncurrent_attempts_conn(
                conn,
                agent_run_id=agent_run_id,
                current_attempt_id=attempt_id,
                current_generation=generation,
                task_run_id=str(run["task_run_id"] or ""),
                now=now,
                reason="takeover_recovery",
                extra_cleanup_attempt_ids=(old_attempt_id,),
            )
            conn.commit()
        row = self.get_attempt(attempt_id)
        assert row is not None
        return row

    # LLM: 仅由已通过原执行锁和代次 CAS 的 create_attempt 调用，旧关闭事实保留在事件账；本方法不授予续跑权。
    # 函数用途: 显式恢复后把执行总账同步为进行中，避免新一轮运行乃至完成时仍挂着上次取消状态。
    def _reopen_task_run_for_attempt_conn(
        self, conn, run, agent_run_id: str, attempt_id: str, now: float,
    ) -> None:
        task_run_id = str(run["task_run_id"] or "")
        previous = conn.execute(
            "SELECT status, closed_at FROM task_runs WHERE task_run_id = ?", (task_run_id,),
        ).fetchone()
        if previous is None or float(previous["closed_at"] or 0) <= 0:
            return
        conn.execute(
            "UPDATE task_runs SET status = 'created', closed_at = 0, updated_at = ? WHERE task_run_id = ?",
            (now, task_run_id),
        )
        self._append_event_conn(
            conn, event_type="task_run.reopened", attempt_id=attempt_id,
            agent_run_id=agent_run_id, task_run_id=task_run_id,
            payload={"previous_status": previous["status"], "previous_closed_at": previous["closed_at"]},
        )

    # ------------------------------------------------------ R1-03 辅助
    def _start_token(self) -> str:
        """本进程 /proc/<pid>/stat 第 22 字段（starttime）——PID 复用防护锚。"""
        try:
            with open(f"/proc/{os.getpid()}/stat", encoding="utf-8") as fh:
                fields = fh.read().split()
            return fields[21] if len(fields) >= 22 else ""
        except OSError:
            return ""

    def _lock_is_takeoverable(self, lock: sqlite3.Row, now: float) -> bool:
        """R1-03 锁接管判定：持主已确认死亡即可立即接管。

        lease 不是进程死亡证明，不能单独触发接管；反过来，PID/start token 已明确
        证明旧 Gateway 死亡时也无需再白等 lease+grace。接管事务会把旧未完成工具
        标 UNKNOWN、mutation 标 DIRTY，避免未知副作用被静默重放。活进程即使模型
        调用长于 lease 仍继续持权；PID/start token 无法确认时一律保守拒绝。
        ``now`` 保留为稳定内部签名，孤儿扫描仍在查询层单独使用 lease+grace 节流。
        """
        return not holder_is_alive(
            int(lock["pid"] or 0),
            str(lock["start_token"] or ""),
        )

    def has_active_exec_lock(self, agent_run_id: str, *, now: float | None = None) -> bool:
        """R1-03 驱动链 lease 感知：run 是否被活跃执行权锁持有。

        活跃 = 锁存在且持主仍存活/无法证死 → worker 在跑，
        发现层不得重复催。判定与 create_attempt 同一把尺（_lock_is_takeoverable）。
        """
        lock = self._runtime_connect().execute(
            "SELECT * FROM resource_locks WHERE canonical_scope = ?",
            (exec_lock_scope(agent_run_id),),
        ).fetchone()
        if lock is None:
            return False
        return not self._lock_is_takeoverable(
            lock, now if now is not None else time.time())

    def _append_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        attempt_id: str,
        agent_run_id: str,
        task_run_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """事务内追加 runtime_events（A.3 append-only，调用方负责 commit）。"""
        conn.execute(
            """
            INSERT INTO runtime_events(event_id, event_type, attempt_id, agent_run_id,
                                      task_run_id, payload_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, event_type, attempt_id, agent_run_id, task_run_id,
             json.dumps(payload or {}, ensure_ascii=False), time.time()),
        )

    def get_attempt(self, attempt_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM agent_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()

    def current_attempt(self, agent_run_id: str) -> sqlite3.Row | None:
        """agent_runs.current_attempt_id 指向的 attempt 行（F.6 验证用）。"""
        with self._runtime_connection() as conn:
            row = conn.execute(
                """
                SELECT a.* FROM agent_attempts a
                JOIN agent_runs r ON r.current_attempt_id = a.attempt_id
                WHERE r.agent_run_id = ?
                """,
                (agent_run_id,),
            ).fetchone()
        return row

    # LLM: Runner result projection must commit only against the exact current
    # attempt and its typed run/attempt statuses; callers must not infer authority
    # from the filesystem task projection or model-authored text.
    # 函数用途: 查询某次子代理回写是否仍属于当前执行轮。
    def runner_result_commit_authority(
        self,
        *,
        run_id: str,
        attempt_id: str,
    ) -> dict[str, object] | None:
        """Return typed commit authority for one exact runner attempt."""
        normalized_run_id = str(run_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        if not normalized_run_id or not normalized_attempt_id:
            return None
        with self._runtime_connection() as conn:
            row = conn.execute(
                """
                SELECT ar.current_attempt_id, ar.current_attempt_generation,
                       ar.status AS run_status,
                       at.attempt_generation, at.status AS attempt_status
                FROM agent_runs ar
                JOIN agent_attempts at ON at.agent_run_id = ar.agent_run_id
                WHERE ar.run_id = ? AND at.attempt_id = ?
                """,
                (normalized_run_id, normalized_attempt_id),
            ).fetchone()
        if row is None:
            return None
        current_attempt_id = str(row["current_attempt_id"] or "")
        current_generation = int(row["current_attempt_generation"] or 0)
        attempt_generation = int(row["attempt_generation"] or 0)
        return {
            "run_id": normalized_run_id,
            "attempt_id": normalized_attempt_id,
            "current_attempt_id": current_attempt_id,
            "current_attempt_generation": current_generation,
            "attempt_generation": attempt_generation,
            "run_status": str(row["run_status"] or ""),
            "attempt_status": str(row["attempt_status"] or ""),
            "is_current": (
                current_attempt_id == normalized_attempt_id
                and current_generation == attempt_generation
            ),
        }

    def attempts_for_run(self, agent_run_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_attempts WHERE agent_run_id = ? ORDER BY attempt_generation",
                (agent_run_id,),
            ).fetchall()
        return list(rows)

    # ----------------------------------------------------------- Delegation
    def create_delegation(
        self,
        *,
        parent_agent_run_id: str,
        child_agent_run_id: str,
        child_attempt_id: str,
        granted_scope: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        """创建 immutable parent→child 委托（A.7）。

        强制 child 已建自己的 attempt（A.6：child 必须先创建 AgentAttempt
        才能被委托/执行工具）；child_attempt_id 必须是 child 的 current attempt。
        """
        with self._runtime_connection() as conn:
            child = conn.execute(
                "SELECT current_attempt_id, current_attempt_generation FROM agent_runs WHERE agent_run_id = ?",
                (child_agent_run_id,),
            ).fetchone()
            if child is None or str(child["current_attempt_id"] or "") != str(child_attempt_id):
                raise RuntimeConflictError(
                    f"委托失败: child {child_agent_run_id} 当前 attempt {child_attempt_id!r} "
                    f"不是其 current pointer"
                )
            delegation_id = new_id("delegation_id")
            conn.execute(
                """
                INSERT INTO delegations(delegation_id, parent_agent_run_id, child_agent_run_id,
                                        child_attempt_id, granted_scope_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    delegation_id,
                    parent_agent_run_id,
                    child_agent_run_id,
                    child_attempt_id,
                    json.dumps(granted_scope or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
            conn.execute(
                "UPDATE agent_runs SET delegation_id = ?, updated_at = ? WHERE agent_run_id = ?",
                (delegation_id, time.time(), child_agent_run_id),
            )
            conn.commit()
        row = self.get_delegation(delegation_id)
        assert row is not None
        return row

    def get_delegation(self, delegation_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM delegations WHERE delegation_id = ?", (delegation_id,)
            ).fetchone()

    def delegation_for_child(self, child_agent_run_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM delegations WHERE child_agent_run_id = ?",
                (child_agent_run_id,),
            ).fetchone()

    # ------------------------------------------------------ WorkspaceBinding
    def create_binding(
        self,
        *,
        agent_run_id: str,
        attempt_id: str,
        owner_id: str,
        root_path: str,
        readable_roots: list[str] | None = None,
        writable_roots: list[str] | None = None,
        extra_write_roots: list[str] | None = None,
        roots_digest: str = "",
        workspace_epoch: int = 1,
    ) -> sqlite3.Row:
        # D.6：相同规范化根集合 digest + 相同 writable 集合 → 复用同一 ACTIVE
        # binding（§5 测试 4：并发相同 root set 只得到一个 ACTIVE binding）。
        writable_json = _sorted_roots_json(writable_roots)
        if roots_digest:
            with self._runtime_connection() as conn:
                existing = conn.execute(
                    """
                    SELECT * FROM workspace_bindings
                    WHERE owner_id = ? AND status = 'ACTIVE'
                      AND roots_digest = ? AND writable_roots_json = ?
                    ORDER BY workspace_epoch DESC LIMIT 1
                    """,
                    (owner_id, roots_digest, writable_json),
                ).fetchone()
            if existing is not None:
                return existing
        binding_id = _binding_id()
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO workspace_bindings(binding_id, owner_id, agent_run_id, attempt_id,
                                               workspace_epoch, root_path,
                                               readable_roots_json, writable_roots_json,
                                               extra_write_roots_json, roots_digest, status,
                                               created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    binding_id,
                    owner_id,
                    agent_run_id,
                    attempt_id,
                    int(workspace_epoch),
                    root_path,
                    _sorted_roots_json(readable_roots),
                    writable_json,
                    _sorted_roots_json(extra_write_roots),
                    roots_digest,
                    now,
                    now,
                ),
            )
            conn.commit()
        row = self.get_binding(binding_id)
        assert row is not None
        return row

    def get_binding(self, binding_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM workspace_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()

    def binding_for_run(self, agent_run_id: str) -> sqlite3.Row | None:
        """agent_run 当前 ACTIVE binding（B.5 授权查询用）。"""
        with self._runtime_connection() as conn:
            return conn.execute(
                """
                SELECT * FROM workspace_bindings
                WHERE agent_run_id = ? AND status = 'ACTIVE'
                ORDER BY workspace_epoch DESC
                LIMIT 1
                """,
                (agent_run_id,),
            ).fetchone()

    def latest_binding_for_run(self, agent_run_id: str) -> sqlite3.Row | None:
        """agent_run 最近一次 binding（不分状态）。

        授权门用它区分「从未建 binding（放行）」与「binding 已 SUPERSEDED
        （D.9 迁移后旧 run fail-closed）」。
        """
        with self._runtime_connection() as conn:
            return conn.execute(
                """
                SELECT * FROM workspace_bindings
                WHERE agent_run_id = ?
                ORDER BY workspace_epoch DESC
                LIMIT 1
                """,
                (agent_run_id,),
            ).fetchone()

    def supersede_binding(
        self,
        *,
        binding_id: str,
        expected_epoch: int,
    ) -> int:
        """D.9：CAS 标记旧 binding SUPERSEDED 并递增 epoch，返回新 epoch。

        F.1：workspace_epoch 只在 binding 迁移/撤销/接管时变化；同一事务内
        联动 agent_runs.workspace_epoch（F.6 fence 校验基准）。迁移流程
        （锁 claims→证明无 active 操作→CAS epoch→标 SUPERSEDED→转移 claims→
        新 binding）由 R2 AttemptExecutionSandbox 编排，本方法只做原子表动作。
        """
        new_epoch = int(expected_epoch) + 1
        now = time.time()
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE workspace_bindings
                SET status = 'SUPERSEDED', workspace_epoch = ?, updated_at = ?
                WHERE binding_id = ? AND status = 'ACTIVE' AND workspace_epoch = ?
                """,
                (new_epoch, now, binding_id, int(expected_epoch)),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"binding 迁移 CAS 失败: {binding_id} epoch {expected_epoch}"
                )
            conn.execute(
                """
                UPDATE agent_runs
                SET workspace_epoch = ?, updated_at = ?
                WHERE agent_run_id = (SELECT agent_run_id FROM workspace_bindings
                                      WHERE binding_id = ?)
                """,
                (new_epoch, now, binding_id),
            )
            conn.commit()
        return new_epoch

    # ------------------------------------------------------------ RootClaims
    def claim_root(
        self,
        *,
        binding_id: str,
        agent_run_id: str,
        attempt_id: str,
        root_path: str,
    ) -> sqlite3.Row:
        """声明物理写根（D.5：同库内唯一）。冲突 → ROOT_CLAIM_CONFLICT。

        R2 起声明前规范化（§5 测试 5）：
        - realpath 归一：symlink 别名指向同一物理路径视为同根；
        - normcase 归一：大小写不敏感文件系统（macOS）别名视为同根；
        - 父子包含关系拒绝：/a 与 /a/b 是同一物理写根区，不能分属两个声明；
        - samefile 探测：既有声明的 mount/别名等价路径视为冲突。

        迁移时旧 claims 必须先被原子转移（D.9），否则新声明必然冲突。
        """
        canonical = _canonical_root_path(root_path)
        claim_id = uuid.uuid4().hex
        try:
            with self._runtime_connection() as conn:
                existing = conn.execute(
                    "SELECT root_path FROM root_claims"
                ).fetchall()
                for row in existing:
                    other = str(row["root_path"] or "")
                    if _roots_overlap(canonical, other):
                        raise RuntimeConflictError(
                            f"{ROOT_CLAIM_CONFLICT}: 物理写根与既有声明重叠: "
                            f"{canonical} vs {other}"
                        )
                conn.execute(
                    """
                    INSERT INTO root_claims(claim_id, binding_id, agent_run_id, attempt_id,
                                            root_path, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (claim_id, binding_id, agent_run_id, attempt_id, canonical, time.time()),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError(
                f"{ROOT_CLAIM_CONFLICT}: 物理写根已被声明: {canonical}"
            ) from exc
        row = self.get_claim(claim_id)
        assert row is not None
        return row

    def get_claim(self, claim_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM root_claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()

    def claim_for_root(self, root_path: str) -> sqlite3.Row | None:
        # 与 claim_root 同一规范化：库里存 canonical，查询别名（大小写/
        # symlink 拼写）必须归一后才能命中自己刚写入的行。
        canonical = _canonical_root_path(root_path)
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM root_claims WHERE root_path = ?", (canonical,)
            ).fetchone()

    def claims_for_binding(self, binding_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM root_claims WHERE binding_id = ?", (binding_id,)
            ).fetchall()
        return list(rows)

    # -------------------------------------------------------- Run 级终态
    # LLM: A model/tool execution slice can finish while its logical task remains
    # resumable. Close only the exact current attempt and keep AgentRun created;
    # a later typed wake must call create_attempt to regain tool authority.
    # 函数用途: 结束一次可续跑的执行片段，释放本轮工具权限但不宣告整个任务完成。
    def settle_agent_attempt(
        self,
        *,
        agent_run_id: str,
        attempt_id: str,
        payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Close one exact running attempt while leaving its AgentRun active."""
        normalized_attempt_id = str(attempt_id or "").strip()
        if not normalized_attempt_id:
            return {"settled": False, "reason": "missing_attempt_id"}
        now = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT ar.task_run_id, ar.current_attempt_id, ar.status AS run_status, "
                "aa.status AS attempt_status, aa.ended_at "
                "FROM agent_runs ar JOIN agent_attempts aa "
                "ON aa.attempt_id = ? AND aa.agent_run_id = ar.agent_run_id "
                "WHERE ar.agent_run_id = ?",
                (normalized_attempt_id, agent_run_id),
            ).fetchone()
            if row is None:
                return {"settled": False, "reason": "no_such_attempt"}
            if str(row["current_attempt_id"] or "") != normalized_attempt_id:
                self._append_event_conn(
                    conn,
                    event_type="closeout_blocked",
                    attempt_id=normalized_attempt_id,
                    agent_run_id=agent_run_id,
                    task_run_id=str(row["task_run_id"] or ""),
                    payload={"reason": "stale_attempt", "scope": "attempt"},
                )
                return {"settled": False, "reason": "stale_attempt"}
            run_status = str(row["run_status"] or "")
            if run_status not in RUN_STATUS_LEGACY_CREATED:
                return {"settled": False, "reason": "run_not_active"}
            attempt_status = str(row["attempt_status"] or "")
            if attempt_status != "running" or float(row["ended_at"] or 0) > 0:
                return {"settled": False, "reason": "already_terminal"}
            active_operations = conn.execute(
                "SELECT COUNT(*) AS count FROM tool_operations "
                "WHERE attempt_id = ? AND status IN (?, ?) AND handler_started_at > 0",
                (normalized_attempt_id, OP_CLAIMED, OP_EXECUTING),
            ).fetchone()
            if int(active_operations["count"] or 0) > 0:
                self._append_event_conn(
                    conn,
                    event_type="closeout_blocked",
                    attempt_id=normalized_attempt_id,
                    agent_run_id=agent_run_id,
                    task_run_id=str(row["task_run_id"] or ""),
                    payload={"reason": "active_tool_operations", "scope": "attempt"},
                )
                return {"settled": False, "reason": "active_tool_operations"}
            _cancel_unstarted_tool_operations(
                conn,
                agent_run_id=agent_run_id,
                attempt_id=normalized_attempt_id,
                now=now,
            )
            updated = conn.execute(
                "UPDATE agent_attempts SET status = 'done', ended_at = ? "
                "WHERE attempt_id = ? AND status = 'running' AND ended_at = 0",
                (now, normalized_attempt_id),
            ).rowcount
            if updated != 1:
                return {"settled": False, "reason": "attempt_cas_conflict"}
            conn.execute(
                "DELETE FROM resource_locks WHERE canonical_scope = ? AND attempt_id = ?",
                (exec_lock_scope(agent_run_id), normalized_attempt_id),
            )
            self._append_event_conn(
                conn,
                event_type="agent_attempt.completed",
                attempt_id=normalized_attempt_id,
                agent_run_id=agent_run_id,
                task_run_id=str(row["task_run_id"] or ""),
                payload={"status": "done", "run_status": run_status, **(payload or {})},
            )
        return {"settled": True, "attempt_id": normalized_attempt_id}

    # LLM: 终态提交与 exact attempt/status CAS 同事务；取消不能清除 UNKNOWN 的锁或恢复障碍。
    # 函数用途: 收口选定执行轮；控制端可要求仍为 pending，避免旧快照误停已开始的新轮。
    def settle_agent_run(
        self,
        *,
        agent_run_id: str,
        status: str,
        payload: dict[str, Any] | None = None,
        now: float | None = None,
        attempt_id: str = "",
        expected_attempt_status: str = "",
    ) -> dict[str, Any]:
        """run 级终态收口（单事务 CAS，幂等，以第一次为准）。

        此前 /ask 路径 agent_runs.status 恒 'created'（生产 UPDATE 只写
        delegation_id/current_attempt_id/workspace_epoch），attempt 恒
        'running'（ended_at=0），runtime_events 无 run 级完成事件——「每次
        可审计」缺 run 级终态。本方法在 run 真实结束时落账：
          - agent_runs.status：'' / 'created' → 终态；已终态 → noop
            （并发/重复收口以第一次为准）；
          - 全部未结 attempt（ended_at=0）：status=终态、ended_at=now；
          - runtime_events 追加 'agent_run.completed'（payload 带终态
            字段），事件绑定 attempt（A.8：每事件追到具体 attempt）。

        R1-03 收口闸（v5）：
          - 终态集校验：status 必须 ∈ {done,failed,cancelled}（集中常量），
            其余（含 unfinished 等 runtime_status 透传）→ 拒 + status_conflict
            诊断事件——unfinished 是任务级可恢复语义，不是 agent_runs.status
            合法写点，历史遗留异常值不得继续由 settle 制造；
          - 未知状态 fail-closed：run 当前状态非 ''/created/终态集 →
            不写 settled，留 status_conflict 事件；
          - stale attempt 闸：传入 attempt_id 时校验其仍是 current
            attempt（takeover 换代后旧 worker 自称收口 → 拒 +
            closeout_blocked 事件）；
          - 取消 UNKNOWN 明确拒绝，不清除既有执行锁或恢复障碍；
          - expected_attempt_status 与 exact attempt 在同一写事务检查；
          - 终态即释放：收口成功同事务释放执行权锁（scope=attempt-exec:
            {agent_run_id}），锁不残留。
        """
        now = time.time() if now is None else now
        event_id = uuid.uuid4().hex
        if expected_attempt_status and not str(attempt_id or "").strip():
            return {"settled": False, "reason": "missing_attempt"}
        if str(status or "").strip() not in AGENT_RUN_TERMINAL_STATUSES:
            with self.transaction() as conn:
                self._append_event_conn(
                    conn, event_type="status_conflict", attempt_id=attempt_id,
                    agent_run_id=agent_run_id,
                    payload={"status": str(status or ""), "action": "settle_blocked",
                             "reason": "invalid_settle_status"},
                )
            return {"settled": False, "reason": "invalid_status"}
        with self.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_run_id, current_attempt_id, status AS run_status "
                "FROM agent_runs WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
            if row is None:
                return {"settled": False, "reason": "no_such_run"}
            cur_status = str(row["run_status"] or "")
            if cur_status not in RUN_STATUS_LEGACY_CREATED and \
                    cur_status not in AGENT_RUN_TERMINAL_STATUSES:
                # 未知状态 fail-closed：不写 settled，留诊断事件
                self._append_event_conn(
                    conn, event_type="status_conflict",
                    attempt_id=str(row["current_attempt_id"] or ""),
                    agent_run_id=agent_run_id,
                    task_run_id=str(row["task_run_id"] or ""),
                    payload={"status": cur_status, "action": "settle_blocked",
                             "reason": "unknown_run_status"},
                )
                return {"settled": False, "reason": "unknown_status"}
            if str(attempt_id or "").strip():
                if str(row["current_attempt_id"] or "") != attempt_id:
                    self._append_event_conn(
                        conn, event_type="closeout_blocked",
                        attempt_id=attempt_id, agent_run_id=agent_run_id,
                        task_run_id=str(row["task_run_id"] or ""),
                        payload={"status": str(status or ""), "reason": "stale_attempt",
                                 "current_attempt_id": str(row["current_attempt_id"] or "")},
                    )
                    return {"settled": False, "reason": "stale_attempt"}
            if str(row["current_attempt_id"] or "").strip():
                attempt_id = str(row["current_attempt_id"])
            else:
                latest = conn.execute(
                    "SELECT attempt_id FROM agent_attempts "
                    "WHERE agent_run_id = ? ORDER BY started_at DESC LIMIT 1",
                    (agent_run_id,),
                ).fetchone()
                attempt_id = str(latest["attempt_id"]) if latest is not None else ""
            attempt = conn.execute(
                "SELECT status FROM agent_attempts WHERE attempt_id = ? AND agent_run_id = ?",
                (attempt_id, agent_run_id),
            ).fetchone()
            attempt_status = str(attempt["status"] or "") if attempt is not None else ""
            if expected_attempt_status and attempt_status != expected_attempt_status:
                return {"settled": False, "reason": "attempt_status_conflict", "attempt_id": attempt_id}
            if status == "cancelled" and attempt_status == ATTEMPT_STATUS_UNKNOWN:
                return {"settled": False, "reason": "attempt_unknown", "attempt_id": attempt_id}
            task_run_id = str(row["task_run_id"])
            cur = conn.execute(
                "UPDATE agent_runs SET status = ?, updated_at = ? WHERE agent_run_id = ? "
                "AND status IN ('', 'created')",
                (status, now, agent_run_id),
            )
            if cur.rowcount == 0:
                return {"settled": False, "reason": "already_terminal"}
            conn.execute(
                "UPDATE agent_attempts SET status = ?, ended_at = ? "
                "WHERE agent_run_id = ? AND ended_at = 0",
                (status, now, agent_run_id),
            )
            # 未启动占位与运行终态同事务结束；公共入口保留原领取事实，保证取消后可精确回读。
            _cancel_unstarted_tool_operations(
                conn,
                agent_run_id=agent_run_id,
                now=now,
            )
            # R1-03：终态即释放执行权锁（不残留，同事务）
            conn.execute(
                "DELETE FROM resource_locks WHERE canonical_scope = ?",
                (exec_lock_scope(agent_run_id),),
            )
            conn.execute(
                """
                INSERT INTO runtime_events(event_id, event_type, attempt_id, agent_run_id,
                                          task_run_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, "agent_run.completed", attempt_id, agent_run_id, task_run_id,
                 json.dumps(payload or {}, ensure_ascii=False), now),
            )
        return {"settled": True, "event_id": event_id, "attempt_id": attempt_id}

    # LLM: 此入口只结束一次 TaskRun 的执行生命周期；不得顺带关闭长期 Task，
    # 也不得从模型正文、验收文案或产物质量推导机器终态。
    # 函数用途: 把已由执行层确认的运行结果幂等写入 TaskRun，并留下关闭事件。
    def settle_task_run_terminal(
        self,
        *,
        task_run_id: str,
        task_id: str = "",
        status: str = "cancelled",
        operator: str = "",
        reason: str = "",
        now: float | None = None,
    ) -> dict[str, object]:
        """将一次 TaskRun 执行投影到终态，供发现层账本自愈。

        Task 是可长期续做的工作身份，本方法只结束当前 TaskRun；它不关闭
        Task，也不读取模型正文、验收清单或产物质量。closed_at=0 才写
        （CAS 幂等，并发/重放以第一次为准）；同时写 task_run.closed 事件，
        与 agent_run.completed 对称可审计。
        """
        if str(status or "").strip() not in AGENT_RUN_TERMINAL_STATUSES:
            return {"settled": False, "reason": "invalid_status"}
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE task_runs SET status = ?, closed_at = ?, updated_at = ? "
                "WHERE task_run_id = ? AND closed_at = 0",
                (status, now, now, task_run_id),
            )
            if cur.rowcount == 0:
                return {"settled": False, "reason": "already_terminal"}
            self._append_event_conn(
                conn,
                event_type="task_run.closed",
                attempt_id="",
                agent_run_id="",
                task_run_id=task_run_id,
                payload={
                    "status": status,
                    "task_id": task_id,
                    "operator": str(operator or ""),
                    "reason": str(reason or ""),
                },
            )
        return {"settled": True, "closed_at": now}

    # LLM: Conversation completion may close a TaskRun only after every AgentRun in its
    # exact tree is terminal; the root AgentRun status supplies the execution outcome.
    # This is lifecycle projection only and must never inspect model prose or task quality.
    # 函数用途: 在普通会话任务已经结构化结束后，确认整棵代理树都结束再原子关闭本次执行。
    def settle_task_run_if_agent_tree_terminal(
        self,
        *,
        task_run_id: str,
        task_id: str = "",
        operator: str = "",
        reason: str = "",
        now: float | None = None,
    ) -> dict[str, object]:
        selected_run = str(task_run_id or "").strip()
        if not selected_run:
            return {"settled": False, "reason": "missing_task_run"}
        now = time.time() if now is None else now
        with self.transaction() as conn:
            task_run = conn.execute(
                "SELECT task_id, closed_at FROM task_runs WHERE task_run_id = ?",
                (selected_run,),
            ).fetchone()
            if task_run is None:
                return {"settled": False, "reason": "no_such_task_run"}
            recorded_task_id = str(task_run["task_id"] or "")
            if str(task_id or "").strip() not in {"", recorded_task_id}:
                return {"settled": False, "reason": "task_mismatch"}
            if float(task_run["closed_at"] or 0.0) > 0:
                return {"settled": False, "reason": "already_terminal"}
            agents = conn.execute(
                "SELECT agent_run_id, parent_agent_run_id, status "
                "FROM agent_runs WHERE task_run_id = ? ORDER BY created_at, agent_run_id",
                (selected_run,),
            ).fetchall()
            if not agents:
                return {"settled": False, "reason": "no_agent_runs"}
            if any(
                str(row["status"] or "") not in AGENT_RUN_TERMINAL_STATUSES
                for row in agents
            ):
                return {"settled": False, "reason": "agent_tree_active"}
            roots = [row for row in agents if not str(row["parent_agent_run_id"] or "")]
            if len(roots) != 1:
                return {"settled": False, "reason": "invalid_agent_tree"}
            root_status = str(roots[0]["status"] or "")
            cur = conn.execute(
                "UPDATE task_runs SET status = ?, closed_at = ?, updated_at = ? "
                "WHERE task_run_id = ? AND closed_at = 0",
                (root_status, now, now, selected_run),
            )
            if cur.rowcount == 0:
                return {"settled": False, "reason": "already_terminal"}
            self._append_event_conn(
                conn,
                event_type="task_run.closed",
                attempt_id="",
                agent_run_id="",
                task_run_id=selected_run,
                payload={
                    "status": root_status,
                    "task_id": recorded_task_id,
                    "operator": str(operator or ""),
                    "reason": str(reason or ""),
                    "root_agent_run_id": str(roots[0]["agent_run_id"] or ""),
                    "agent_run_count": len(agents),
                },
            )
        return {"settled": True, "closed_at": now, "status": root_status}

    # ------------------------------------------- R1-03 收口矩阵 + 孤儿兜底
    # LLM: 这里只依据 tool operation 的结构化执行事实分类；UNKNOWN/EXECUTING
    # 必须停手，业务完成度和最终答复由模型主链处理。
    # 函数用途: 判断一次尝试能否安全归为完成、失败、取消或暂不裁决。
    def classify_attempt_closeout(self, attempt_id: str) -> str | None:
        """R1-03 收口矩阵五档（v5，纯结构化 op 状态分类）。

          - 无 op 痕迹          → 'cancelled'（纯账本收口，无副作用可证明）
          - 全 SUCCEEDED/CANCELLED → 'done'
          - 含 FAILED           → 'failed'
          - 含 EXECUTING/UNKNOWN → None（停手，绝不自动裁决——可能已生效，
            结果不可知，无法证明零副作用）
          - CLAIMED 且 handler_started_at=0 → 未启动（not_started，G.5
            CANCELLED 语义：副作用可证明未发生）→ 不阻塞收口
        2026-08-15 真机根因（3×3 cell2 两轮同构）：模型输出未闭合
        [TOOL_CALL] 坏块（TOOL_CALL_UNCLOSED）→ 整轮零执行（J.5 安全设计）
        → 该轮 op 停在 CLAIMED 且从未启动（45/45 实证 handler_started_at=0）
        → 旧逻辑把它与可能已生效的 EXECUTING/UNKNOWN 同等判 None → 收口
        标 TOOL_OPERATION_OUTCOME_UNKNOWN → 任务 failed。未启动 op 的
        副作用可证明未发生，不该拖死可续跑任务。
        这里只裁决 attempt 的客观执行痕迹；最终回复和投递走各自现有主链，
        不再经过已退休的机器验收合同。
        """
        with self._runtime_connection() as conn:
            attempt = conn.execute(
                "SELECT * FROM agent_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt is None:
                return None
            rows = conn.execute(
                "SELECT status, handler_started_at, outcome_json FROM tool_operations "
                "WHERE attempt_id = ?", (attempt_id,),
            ).fetchall()
        if not rows:
            return "cancelled"
        for op in rows:
            op_status = str(op["status"] or "")
            if op_status == OP_CLAIMED:
                if float(op["handler_started_at"] or 0) <= 0:
                    continue  # 未启动 = 可证明零副作用（G.5）
                return None  # 已启动的 CLAIMED 异常（理论不存在）仍 fail-closed
            if op_status in {OP_EXECUTING, OP_UNKNOWN}:
                return None  # 停手——可能已生效
            if op_status == OP_FAILED:
                return "failed"
        return "done"

    def _mark_attempt_unknown(
        self, attempt_id: str, agent_run_id: str, *, reason: str, operator: str
    ) -> bool:
        """attempt 标结构化 unknown 终态（CAS 幂等，单事务）。

        2026-08-15 双席核对点3/证据4: 执行者死亡 + 结果不可知 → attempt
        层终态标记 (status='unknown' + ended_at), 绝不等同 failed(已知失败),
        也绝不触发自动续跑(执行权锁保留 + create_attempt 的 unknown 闸
        fail-closed 拒绝——recovered 前任何自动拉起都会撞 RuntimeConflictError)。
        四层保持分开: 只动 agent_attempts, run/task 层状态不落账。
        """
        now = time.time()
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    "UPDATE agent_attempts SET status = ?, ended_at = ? "
                    "WHERE attempt_id = ? AND status = 'running'",
                    (ATTEMPT_STATUS_UNKNOWN, now, attempt_id),
                )
                if cur.rowcount == 0:
                    return False  # 已被其他路径终态化, 幂等跳过
                self._append_event_conn(
                    conn,
                    event_type="attempt_unknown_terminal",
                    attempt_id=attempt_id,
                    agent_run_id=agent_run_id,
                    payload={
                        "status": ATTEMPT_STATUS_UNKNOWN,
                        "reason": str(reason or ""),
                        "operator": str(operator or ""),
                        "hint": "执行者死亡且结果不可知, 自动回收停手; 执行权锁保留, "
                                "恢复器须显式 recover_attempt_unknown 核对后释放",
                    },
                )
            return True
        except Exception:  # noqa: BLE001 标记尽力而为, 不改变 fail-closed 语义
            return False

    def find_orphaned_attempts(
        self,
        *,
        now: float | None = None,
        grace_seconds: int | None = None,
    ) -> list[sqlite3.Row]:
        """R1-03 孤儿 attempt 兜底：执行权锁过期超出宽限期且 run 非终态。

        判据（v5 四件套之 1/3）：锁 lease_expires_at < now-grace（grace 默认
        2×lease）、PID/start token 明确证明持主已死，且 run status ∈
        {''/created}。lease 过期但进程仍活不是孤儿；run 已终态的残留锁也不
        重复收口。
        """
        now = time.time() if now is None else now
        grace = max(0, int(grace_seconds or EXEC_LOCK_GRACE_SECONDS))
        with self._runtime_connection() as conn:
            locks = conn.execute(
                "SELECT * FROM resource_locks "
                "WHERE canonical_scope LIKE ? AND lease_expires_at < ?",
                (f"{EXEC_LOCK_SCOPE_PREFIX}%", now - grace),
            ).fetchall()
            orphans: list[sqlite3.Row] = []
            for lock in locks:
                run = conn.execute(
                    "SELECT status FROM agent_runs WHERE agent_run_id = ?",
                    (str(lock["canonical_scope"]).removeprefix(EXEC_LOCK_SCOPE_PREFIX),),
                ).fetchone()
                if run is None or str(run["status"] or "") not in RUN_STATUS_LEGACY_CREATED:
                    continue  # 终态/未知状态 run：不兜底（fail-closed）
                if holder_is_alive(
                    int(lock["pid"] or 0),
                    str(lock["start_token"] or ""),
                ):
                    continue  # lease 过期不是死亡证明；活 worker 仍拥有本 attempt
                orphans.append(lock)
        return orphans

    def reclaim_orphaned_attempt(
        self,
        attempt_id: str,
        *,
        operator: str,
        reason: str = "orphan_reclaim",
    ) -> dict[str, Any]:
        """R1-03 显式回收孤儿 attempt（人工/恢复器入口，普通 wake 不构成）。

        判 failed 前置副作用门（v5 四件套之 4）：attempt 有外部副作用 op
        且无 effect_key（无法验证幂等）→ 拒自动判 failed，交人工
        （fail-closed——外部副作用结果不可知时绝不自动收口）。

        回收动作 = settle_agent_run（终态集校验 + stale 闸 + 终态释放锁
        全部复用；run 非终态才可能成功——CAS 以第一次为准）。
        """
        now = time.time()
        with self._runtime_connection() as conn:
            lock = conn.execute(
                "SELECT * FROM resource_locks WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if lock is None:
                return {"reclaimed": False, "reason": "no_exec_lock"}
            # LLM: 扫描与回收入口必须重复同一双条件，避免扫描后 holder 仍活
            # 却因 lease 单独过期被收口。显式调用也不能绕过死亡证明。
            # 函数用途: 只有超宽限期并确认进程死亡的锁才进入孤儿裁决。
            if not self._lock_is_takeoverable(lock, now):
                return {"reclaimed": False, "reason": "lock_active"}
            # 幂等闭环(2026-08-15): attempt 已标 unknown/recovered 终态 →
            # 不再重复扫描/重复标/重复写事件(孤儿回收每 ~5 分钟一趟)。
            attempt_row = conn.execute(
                "SELECT status FROM agent_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt_row is not None and \
                    str(attempt_row["status"] or "") in _ATTEMPT_TERMINAL_STATUSES:
                return {"reclaimed": False, "reason": "already_terminal"}
            rows = conn.execute(
                "SELECT outcome_json FROM tool_operations WHERE attempt_id = ? "
                "AND status IN ('EXECUTING', 'CLAIMED', 'UNKNOWN')",
                (attempt_id,),
            ).fetchall()
        for op in rows:
            try:
                payload = json.loads(op["outcome_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict) and payload.get("side_effect") is True \
                    and not str(payload.get("effect_key") or "").strip():
                # 问题C(2026-08-14 真机实证): 副作用门拦截曾是静默的——attempt
                # 永卡 running 且无任何可见信号(第3轮复刻任务卡死 30 分钟无感知)。
                # 拦截转可见: 写 runtime_events 审计事件, owner/恢复器可发现并
                # 人工处理; 不改 fail-closed 语义(外部副作用结果不可知时绝不
                # 自动收口, 安全意图保持)。
                # 幂等(双席复核 seq1947): 同 attempt+reason 只写一次, 防
                # 每 ~5 分钟扫描周期重复刷事件。
                try:
                    already = self._runtime_connect().execute(
                        "SELECT 1 FROM runtime_events WHERE event_type = 'orphan_reclaim_blocked' "
                        "AND attempt_id = ? AND payload_json LIKE ? LIMIT 1",
                        (attempt_id, "%\"reason\": \"side_effect_gate\"%"),
                    ).fetchone()
                    if already is None:
                        self.append_event(
                            event_type="orphan_reclaim_blocked",
                            attempt_id=attempt_id,
                            agent_run_id=str(lock["canonical_scope"] or "").removeprefix(
                                EXEC_LOCK_SCOPE_PREFIX
                            ),
                            payload={
                                "reason": "side_effect_gate",
                                "operator": str(operator or ""),
                                "hint": "attempt 有外部副作用且无 effect_key, 自动回收被拒; "
                                        "请人工核对后处理",
                            },
                        )
                except Exception:
                    pass  # 事件写入尽力而为, 不改变拦截语义
                # 2026-08-15 双席核对点3: 拦截转结构化终态——attempt 标 unknown
                # + 执行权锁保留(外部副作用未核实, 锁防并发写; create_attempt
                # 的 unknown 闸拦自动接管), 人工 recover 核对副作用后才释放。
                marked = self._mark_attempt_unknown(
                    attempt_id,
                    str(lock["canonical_scope"] or "").removeprefix(EXEC_LOCK_SCOPE_PREFIX),
                    reason="side_effect_gate",
                    operator=operator,
                )
                return {"reclaimed": False, "reason": "side_effect_gate",
                        "marked_unknown": marked}
        run = self.get_attempt(attempt_id)
        if run is None:
            return {"reclaimed": False, "reason": "no_such_attempt"}
        closeout = self.classify_attempt_closeout(attempt_id)
        if closeout is None:
            # 2026-08-15 真机(verify-mr4 attempt-10 僵尸坐实): UNKNOWN op
            # fail-closed 拒自动裁决是安全设计(结果不可知绝不盲判), 但此前
            # 此路径静默返回——attempt 永久 running 且无任何可见信号(问题C
            # 只补了 side_effect_gate 的可见性, 漏了 nonterminal_ops)。
            # 补可见: 写 runtime_events 审计事件, owner/恢复器可发现并人工
            # 处理(长期助手 同款 unknown 语义: 执行者死亡、副作用是否发生
            # 未知——诚实标注而非永久 running)。
            # 幂等(双席复核 seq1947 核对点2): 同 attempt+reason 的 blocked
            # 事件只写一次——孤儿回收每 ~5 分钟一趟, 不幂等会每周期刷一条
            # 重复事件污染账本。已有同型事件 → 跳过本次写入。
            try:
                already = self._runtime_connect().execute(
                    "SELECT 1 FROM runtime_events WHERE event_type = 'orphan_reclaim_blocked' "
                    "AND attempt_id = ? AND payload_json LIKE ? LIMIT 1",
                    (attempt_id, "%\"reason\": \"nonterminal_ops\"%"),
                ).fetchone()
                if already is None:
                    self.append_event(
                        event_type="orphan_reclaim_blocked",
                        attempt_id=attempt_id,
                        agent_run_id=str(lock["canonical_scope"] or "").removeprefix(
                            EXEC_LOCK_SCOPE_PREFIX
                        ),
                        payload={
                            "reason": "nonterminal_ops",
                            "operator": str(operator or ""),
                            "hint": "attempt 含结果未知(UNKNOWN)工具操作, 自动回收被拒; "
                                    "执行者可能已消失但副作用状态不可知——请人工核对后处理",
                        },
                    )
            except Exception:
                pass  # 事件写入尽力而为, 不改变拦截语义
            # 2026-08-15 双席核对点3: 拦截转结构化终态——attempt 标 unknown
            # + 执行权锁保留(UNKNOWN op 结果不可知; create_attempt 的 unknown
            # 闸拦自动接管——recovered 前该 run 不会被自动拉起续跑)。
            marked = self._mark_attempt_unknown(
                attempt_id,
                str(run["agent_run_id"] or ""),
                reason="nonterminal_ops",
                operator=operator,
            )
            return {"reclaimed": False, "reason": "nonterminal_ops",
                    "marked_unknown": marked}
        result = self.settle_agent_run(
            agent_run_id=str(run["agent_run_id"]),
            status=closeout,
            attempt_id=attempt_id,
            payload={"status": closeout, "reason": reason,
                     "operator": str(operator or "")},
        )
        if not result.get("settled"):
            return {"reclaimed": False, "reason": result.get("reason", "settle_failed")}
        return {"reclaimed": True, "status": closeout, "attempt_id": attempt_id}

    # LLM: 这是 unknown 执行权的唯一人工出口；恢复 current attempt 时必须同时
    # 恢复 startup recovery 写入的 unknown run，并只释放该 attempt 自己的锁。
    # 函数用途: 人工核对副作用处置后恢复同一主链，使保留的事件可以重新调度。
    def recover_attempt_unknown(
        self,
        attempt_id: str,
        *,
        operator: str,
        effect_disposition: str = "confirmed_noop",
        reason: str = "",
    ) -> dict[str, Any]:
        """人工核对后的显式恢复（2026-08-15 双席证据4）：unknown → recovered。

        UNKNOWN 终态的 attempt 只有这一条出口：恢复器（人/监督者）核对外部
        副作用处置后显式调用——事务内 CAS（status='unknown' → 'recovered'）
        成功才释放执行权锁（UNIQUE scope 允许新 attempt 挂载）+ 写
        attempt_recovered 审计事件（含 operator/effect_disposition/reason）。
        非 unknown 状态/已 recovered → 拒绝（幂等，不重复删锁/写事件）。
        effect_disposition 结构化值：confirmed_noop(已核实无副作用) /
        recorded(副作用已人工入账) / abandoned(副作用丢弃接受)。禁 NL 判定。
        """
        if str(effect_disposition or "") not in {"confirmed_noop", "recorded", "abandoned"}:
            return {"recovered": False, "reason": "invalid_effect_disposition"}
        with self.transaction() as conn:
            return _recover_unknown_attempt_conn(
                self,
                conn,
                attempt_id=attempt_id,
                operator=operator,
                effect_disposition=effect_disposition,
                reason=reason,
            )

    # -------------------------------------------------------- wake_queue
    # LLM: 调度唤醒字条(2026-08-17 扫描治理 owner 拍板)——"任务自己留的闹钟"。
    # 每任务一行待醒字条(upsert 幂等); hot tick 只 pop 到期行(索引查询);
    # 事件(用户消息/子代理完成)可提前置醒; woke 后由消费者核任务档案再拉
    # 起, 档案证明已终结 → 清行(僵尸即清, 不累积)。
    def upsert_wake(
        self,
        *,
        root_task_id: str,
        next_due_at: float,
        kind: str = "sleep",
        root_run_id: str = "",
        root_thread_id: str = "",
    ) -> sqlite3.Row:
        """写/更新一个任务的唤醒字条(同一任务只保留一条待醒行)。"""
        now = time.time()
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT wake_id FROM wake_queue WHERE root_task_id = ? "
                "AND status != 'woke' ORDER BY created_at DESC LIMIT 1",
                (str(root_task_id),),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE wake_queue SET next_due_at = ?, kind = ?, "
                    "root_run_id = ?, root_thread_id = ?, updated_at = ? "
                    "WHERE wake_id = ?",
                    (
                        float(next_due_at), str(kind), str(root_run_id),
                        str(root_thread_id), now, str(row["wake_id"]),
                    ),
                )
                conn.commit()
                return conn.execute(
                    "SELECT * FROM wake_queue WHERE wake_id = ?",
                    (str(row["wake_id"]),),
                ).fetchone()
            wake_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO wake_queue(wake_id, root_task_id, root_run_id, "
                "root_thread_id, kind, next_due_at, status, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    wake_id, str(root_task_id), str(root_run_id),
                    str(root_thread_id), str(kind), float(next_due_at), now, now,
                ),
            )
            conn.commit()
            return conn.execute(
                "SELECT * FROM wake_queue WHERE wake_id = ?", (wake_id,)
            ).fetchone()

    def pop_due_wakes(self, *, now: float, limit: int = 64) -> list[dict[str, Any]]:
        """取出到期的待醒字条(CAS 标 claimed, 防双叫); 只索引查询不翻目录。

        WK-INT(2026-08-20): 一次性闹钟 → 持久化状态机。到期行置 claimed 并
        带 lease_until(消费方在 lease 内执行, 失败 release 回 pending 重试,
        进程崩溃后 lease 过期由 reclaim_expired_wakes 回收)——wake 不再
        "pop 即没", 失败可重试、崩溃可恢复。选+标在同一事务内完成。"""
        current = float(now)
        lease_until = current + 300.0  # 5 分钟执行窗(与调度 tick 周期匹配)
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM wake_queue WHERE status = 'pending' "
                "AND next_due_at <= ? AND retry_after <= ? "
                "ORDER BY next_due_at LIMIT ?",
                (current, current, int(limit)),
            ).fetchall()
            if rows:
                ids = [str(row["wake_id"]) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE wake_queue SET status = 'claimed', woke_at = ?, "
                    f"lease_until = ? WHERE wake_id IN ({placeholders}) "
                    f"AND status = 'pending'",
                    (current, lease_until, *ids),
                )
                conn.commit()
            popped = [dict(row) for row in rows]
            for item in popped:
                item["status"] = "claimed"
                item["woke_at"] = current
                item["lease_until"] = lease_until
            return popped

    def complete_wake(self, wake_id: str) -> bool:
        """字条处理完毕(任务已拉起/已确认终结)——删除该行(终态无保留价值)。"""
        with self._runtime_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM wake_queue WHERE wake_id = ?", (str(wake_id),)
            )
            conn.commit()
            return cursor.rowcount > 0

    def release_wake(
        self,
        wake_id: str,
        *,
        retry_after: float,
        last_error: str = "",
    ) -> bool:
        """WK-INT: 执行失败/429 限流 → 回 pending 并设重试点(带退避)。

        不丢 wake: 失败保留记录, retry_after 之后重新 due。attempt_count
        累计供审计/退避升级。"""
        current = time.time()
        with self._runtime_connection() as conn:
            cursor = conn.execute(
                "UPDATE wake_queue SET status = 'pending', retry_after = ?, "
                "attempt_count = attempt_count + 1, last_error = ?, "
                "updated_at = ? WHERE wake_id = ? AND status = 'claimed'",
                (float(retry_after), str(last_error)[:500], current, str(wake_id)),
            )
            conn.commit()
            return cursor.rowcount > 0

    def reclaim_expired_wakes(self, *, now: float) -> int:
        """WK-INT: 崩溃/卡死回收——claimed 但 lease 过期的行回 pending 重试。"""
        current = float(now)
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT wake_id FROM wake_queue WHERE status = 'claimed' "
                "AND lease_until < ?",
                (current,),
            ).fetchall()
            if not rows:
                return 0
            ids = [str(row["wake_id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE wake_queue SET status = 'pending', retry_after = ?, "
                f"attempt_count = attempt_count + 1, "
                f"last_error = 'lease_expired_reclaim', updated_at = ? "
                f"WHERE wake_id IN ({placeholders}) AND status = 'claimed'",
                (current + 1.0, current, *ids),
            )
            conn.commit()
            return len(ids)

    def cancel_wakes_for_task(self, root_task_id: str) -> int:
        """任务终止/接管时清掉它的全部字条, 返回删除行数。"""
        with self._runtime_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM wake_queue WHERE root_task_id = ?",
                (str(root_task_id),),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def list_pending_wakes(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """当前待醒字条清单(对账用: 同步"等待者名单"与任务档案)。"""
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM wake_queue WHERE status = 'pending' LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def stale_wakes(self, *, now: float, woke_timeout_seconds: float) -> list[dict[str, Any]]:
        """claimed 超过超时仍未 complete 的字条(叫了没人干完)——僵尸候补,
        消费者逐条核任务档案后清行。WK-INT: 状态机化后 claimed 取代 woke。"""
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM wake_queue WHERE status = 'claimed' AND woke_at > 0 "
                "AND woke_at <= ?",
                (float(now) - float(woke_timeout_seconds),),
            ).fetchall()
            return [dict(row) for row in rows]

    # -------------------------------------------------------- RuntimeEvents
    def append_event(
        self,
        *,
        event_type: str,
        attempt_id: str,
        agent_run_id: str,
        task_run_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        """append-only 权威事件（A.3/A.8：每事件追到具体 attempt）。"""
        event_id = uuid.uuid4().hex
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO runtime_events(event_id, event_type, attempt_id, agent_run_id,
                                          task_run_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    attempt_id,
                    agent_run_id,
                    task_run_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
            conn.commit()
        return conn_row_to_event(self._fetch_event(event_id))

    def _fetch_event(self, event_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM runtime_events WHERE event_id = ?", (event_id,)
            ).fetchone()

    def events_for_attempt(self, attempt_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_events WHERE attempt_id = ?
                ORDER BY seq ASC LIMIT ?
                """,
                (attempt_id, int(limit)),
            ).fetchall()
        return [conn_row_to_event(row) for row in rows]

    # LLM: 恢复游标只决定扫描顺序，不代表消费；消费由同 agent_run/attempt 的独立回执决定。
    # 每页在未消费集合内按 seq 前进，到尾部再绕回，坏记录不会饿死后面的记录；不删除事件。
    # 函数用途: 分页轮转读取待恢复事实，并持久化下一次扫描位置；同一身份只返回最新事实。
    def pending_events_page(
        self, event_type: str, *, consumed_event_type: str, consumer: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        key = f"event_recovery_cursor:{consumer}:{event_type}"
        with self.transaction() as conn:
            saved = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            cursor = int(saved["value"]) if saved is not None else 0
            query = """
                SELECT e.* FROM runtime_events e
                WHERE e.event_type = ? AND e.seq > ?
                  AND NOT EXISTS (
                    SELECT 1 FROM runtime_events c WHERE c.event_type = ?
                    AND c.agent_run_id = e.agent_run_id AND c.attempt_id = e.attempt_id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM runtime_events n WHERE n.event_type = e.event_type
                    AND n.agent_run_id = e.agent_run_id AND n.attempt_id = e.attempt_id
                    AND n.seq > e.seq
                  )
                ORDER BY e.seq ASC LIMIT ?
            """
            rows = conn.execute(
                query, (event_type, cursor, consumed_event_type, max(1, int(limit))),
            ).fetchall()
            if not rows and cursor:
                rows = conn.execute(
                    query, (event_type, 0, consumed_event_type, max(1, int(limit))),
                ).fetchall()
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(rows[-1]["seq"] if rows else 0)),
            )
        return [conn_row_to_event(row) for row in rows]


def conn_row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "attempt_id": row["attempt_id"],
        "agent_run_id": row["agent_run_id"],
        "task_run_id": row["task_run_id"],
        "payload": json.loads(row["payload_json"] or "{}"),
        "created_at": row["created_at"],
    }


def _binding_id() -> str:
    # binding 不在 B.1 七类内（B.1 只列 run/task/taskrun/agentrun/attempt/session/
    # delegation），用无前缀 uuid 防与框架 ID 混淆。
    return uuid.uuid4().hex


def _sorted_roots_json(roots: list[str] | None) -> str:
    """根集合规范化 JSON：排序后序列化（D.6 digest 比较与顺序无关）。"""
    return json.dumps(sorted(str(r).strip() for r in (roots or []) if str(r).strip()), ensure_ascii=False)


def _canonical_root_path(root_path: str) -> str:
    """root claim 规范化：realpath（symlink 别名）+ 大小写别名归一。

    normcase 在 POSIX 是 no-op（大小写折叠只在 Windows），macOS 的
    大小写不敏感须按文件系统实测：目标父目录大小写不敏感（探测同文件
    大小写拼写等价）→ 路径转小写；Linux 大小写敏感 → 保持原样
    （/A 与 /a 是真实不同路径，不得归一）。
    """
    try:
        path = Path(str(root_path or "")).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        path = Path(str(root_path or ""))
    if _fs_is_case_insensitive(path):
        return str(path).lower()
    return str(path)


def _fs_is_case_insensitive(path: Path) -> bool:
    """实测 path 所在文件系统是否大小写不敏感（macOS 默认卷 / Windows）。

    大小写敏感性是卷属性、与具体目录无关；path 的父目录可能尚未创建
    （claim 的子根），此时向上找最近的存在祖先探测，保证同一卷上所有
    路径的归一结果一致。
    """
    parent = path.parent
    while parent and not parent.exists():
        parent = parent.parent
    if not parent:
        return False
    probe = parent / f".caseprobe-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    try:
        probe.write_text("x", encoding="utf-8")
        upper = parent / probe.name.upper()
        same = upper.exists() if upper != probe else False
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return same


def _roots_overlap(canonical: str, other: str) -> bool:
    """两规范化写根是否重叠（测试 5）：相等/父子包含/samefile 别名。"""
    if not other:
        return False
    if canonical == other:
        return True
    left = canonical.rstrip("/")
    right = other.rstrip("/")
    if left.startswith(right + "/") or right.startswith(left + "/"):
        return True
    try:
        # mount/绑定别名：不同路径字符串同一物理文件/目录。
        if os.path.exists(canonical) and os.path.exists(other) and os.path.samefile(canonical, other):
            return True
    except OSError:
        pass
    return False
