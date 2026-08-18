"""Owner runtime.db 权威实体仓储（3.txt A/C/D/F 节落地面）。

职责：
- 主链（A.4）：Task → TaskRun → AgentRun 树 → AgentAttempt 的创建与读取。
- 不可变委托（A.7）：child 必须先建自己的 AgentAttempt 才能被 delegate。
- current attempt pointer（F.2/F.3）：create_attempt 以 CAS 递增 generation
  并替换 agent_runs.current_attempt_id；旧 attempt 行是不可变历史（F.7）。
- WorkspaceBinding / root claims（D 节）：绑定承载可读写根集合与 epoch，
  同一物理写根在库内唯一声明（D.5）。
- runtime_events（A.3）：append-only 权威事件流，每事件追到 attempt（A.8）。

所有 ID 由 B.1 统一生成器（common.id_generator.new_id）铸造，本模块不手拼。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import socket

from ..common.id_generator import new_id
from .acceptance_operations import RuntimeAcceptanceMixin
from .delivery_operations import RuntimeDeliveryMixin
from .operations import (
    AGENT_RUN_TERMINAL_STATUSES,
    ATTEMPT_STATUS_RECOVERED,
    ATTEMPT_STATUS_UNKNOWN,
    EXEC_LOCK_GRACE_SECONDS,
    EXEC_LOCK_LEASE_SECONDS,
    EXEC_LOCK_SCOPE_PREFIX,
    OP_CANCELLED,
    OP_CLAIMED,
    OP_EXECUTING,
    OP_FAILED,
    OP_UNKNOWN,
    RUN_STATUS_LEGACY_CREATED,
    RuntimeConflictError,
    RuntimeOperationsMixin,
    _ATTEMPT_TERMINAL_STATUSES,
    exec_lock_scope,
    holder_is_alive,
)
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


class RuntimeRepository(
    RuntimeSchemaMixin,
    RuntimeOperationsMixin,
    RuntimeAcceptanceMixin,
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
    ) -> dict[str, str]:
        """create_run 权威主链写入（单事务）。

        一个 TaskRun（=一次执行）→ 一棵 root/child AgentRun 树（A.4/A.5：
        一次 TaskRun 下一棵根树；每个 TaskRun 创建一个 root AgentRun）→
        每个 AgentRun 第一个 AgentAttempt（A.6：执行任何工具前必须有
        attempt）→ current pointer（F.3 初值 generation 1）。
        conversation_task_id 与 thread_id 在同一事务进入 tasks 行（A.9：
        ConversationTaskLink 不再是独立权威）。

        F9（A.4 树建模）：child（parent 有权威记录）并入 parent 的 TaskRun，
        不新建 TaskRun/Task——树的 AgentRun 同属一个 TaskRun，链是
        Task→TaskRun→AgentRun tree→AgentAttempt；child 经 immutable
        Delegation 连接 parent AgentRun（A.7）。parent 无权威记录
        （R1 前存量/旁路）时 child 自成新树根（parent/delegation 为空 =
        合法根身份 A.7），不阻断新链。
        """
        with self.transaction() as conn:
            now = time.time()
            task_run_id = ""
            parent_agent_run_id = ""
            task_id = ""
            if str(parent_run_id or "").strip():
                # child：并入 parent 的 TaskRun（同树），共享 parent 的 Task 身份。
                parent_row = conn.execute(
                    "SELECT ar.agent_run_id, ar.task_run_id, tr.task_id "
                    "FROM agent_runs ar "
                    "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id "
                    "WHERE ar.run_id = ?",
                    (parent_run_id,),
                ).fetchone()
                if parent_row is not None:
                    parent_agent_run_id = str(parent_row["agent_run_id"])
                    task_run_id = str(parent_row["task_run_id"])
                    task_id = str(parent_row["task_id"] or "")
            if not task_run_id:
                # root（无 parent 或 parent 无权威记录）：新 TaskRun + Task 行。
                # Task：conversation_task_id 复用既有任务身份；无则铸造框架 task_id。
                task_id = str(conversation_task_id or "").strip() or new_id("task_id")
                existing = conn.execute(
                    "SELECT task_id, owner_id FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if existing is None:
                    try:
                        conn.execute(
                            """
                            INSERT INTO tasks(task_id, owner_id, thread_id, conversation_task_id,
                                              title, goal, status, created_at, updated_at)
                            VALUES(?, ?, ?, ?, '', ?, 'active', ?, ?)
                            """,
                            (task_id, owner_id, thread_id, conversation_task_id, goal, now, now),
                        )
                    except sqlite3.IntegrityError:
                        # 并发同 conversation 建任务：另一事务已插入，取既有行。
                        pass
                task_run_id = new_id("task_run_id")
                conn.execute(
                    """
                    INSERT INTO task_runs(task_run_id, task_id, status, created_at, updated_at, metadata_json)
                    VALUES(?, ?, 'created', ?, ?, '{}')
                    """,
                    (task_run_id, task_id, now, now),
                )
            # root AgentRun（parent 为空 = 合法根身份 A.7）。
            agent_run_id = new_id("agent_run_id")
            delegation_id = ""
            conn.execute(
                """
                INSERT INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id,
                                       delegation_id, run_id, role, status,
                                       current_attempt_id, current_attempt_generation,
                                       workspace_epoch, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, 'created', '', 0, 1, ?, ?)
                """,
                (agent_run_id, task_run_id, parent_agent_run_id, delegation_id,
                 run_id, role, now, now),
            )
            # 第一个 AgentAttempt（A.6），CAS 语义与 create_attempt 同源。
            attempt_id = new_id("attempt_id")
            conn.execute(
                """
                INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation,
                                           status, started_at)
                VALUES(?, ?, 1, 'running', ?)
                """,
                (attempt_id, agent_run_id, now),
            )
            conn.execute(
                """
                UPDATE agent_runs
                SET current_attempt_id = ?, current_attempt_generation = 1, updated_at = ?
                WHERE agent_run_id = ? AND current_attempt_generation = 0
                """,
                (attempt_id, now, agent_run_id),
            )
            # child：immutable 委托（A.7）。
            if parent_agent_run_id:
                delegation_id = new_id("delegation_id")
                conn.execute(
                    """
                    INSERT INTO delegations(delegation_id, parent_agent_run_id, child_agent_run_id,
                                            child_attempt_id, granted_scope_json, created_at)
                    VALUES(?, ?, ?, ?, '{}', ?)
                    """,
                    (delegation_id, parent_agent_run_id, agent_run_id, attempt_id, now),
                )
                conn.execute(
                    "UPDATE agent_runs SET delegation_id = ? WHERE agent_run_id = ?",
                    (delegation_id, agent_run_id),
                )
            # A.8：事件追到 attempt。
            for event_type in ("task.created", "agent_run.created"):
                conn.execute(
                    """
                    INSERT INTO runtime_events(event_id, event_type, attempt_id, agent_run_id,
                                              task_run_id, payload_json, created_at)
                    VALUES(?, ?, ?, ?, ?, '{}', ?)
                    """,
                    (uuid.uuid4().hex, event_type, attempt_id, agent_run_id, task_run_id, now),
                )
        return {
            "task_id": task_id,
            "task_run_id": task_run_id,
            "agent_run_id": agent_run_id,
            "attempt_id": attempt_id,
            "delegation_id": delegation_id,
        }

    # ------------------------------------------------------------------ Task
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
                                  title, goal, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
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

    # ---------------------------------------------------------- AgentAttempt
    def create_attempt(self, agent_run_id: str) -> sqlite3.Row:
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
          - 他人持有且活跃 → RuntimeConflictError（并发双挂载恰一成功）；
          - 过期或持主判死 → 接管（删旧锁建新锁）。
        锁释放四元组（scope+holder_instance+attempt_id+attempt_generation），
        settle 终态即释放；takeover/接管同事务删旧锁。

        CAS 换代（F.2/F.3）：0 行命中 = 并发冲突，fail-closed；旧 attempt
        行不可变（F.7）。G4 takeover（G4-4）原子收尾：旧 attempt 非终态
        operation 统一转 UNKNOWN、mutation 标 DIRTY（旧 worker 即使还活着
        也只能看到 UNKNOWN/DIRTY，无法盲重放）。
        """
        now = time.time()
        scope = exec_lock_scope(agent_run_id)
        with self._runtime_connection() as conn:
            run = conn.execute(
                "SELECT current_attempt_generation, current_attempt_id, "
                "status, task_run_id, workspace_epoch FROM agent_runs "
                "WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"agent_run 不存在: {agent_run_id}")
            status = str(run["status"] or "")
            # 终态（done/failed/cancelled）挂载放行：见 docstring 防线分层——
            # 自喂防护在发现层过滤 + 执行权锁，任务级生命周期闸裁决「该不该重跑」。
            # 合法状态 = 现役（''/created）+ 全部终态；其余（历史 unfinished 等
            # 非法值）→ fail-closed。
            if status not in RUN_STATUS_LEGACY_CREATED and \
                    status not in AGENT_RUN_TERMINAL_STATUSES:
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
            latest_attempt = conn.execute(
                "SELECT attempt_id, status FROM agent_attempts "
                "WHERE agent_run_id = ? ORDER BY started_at DESC LIMIT 1",
                (agent_run_id,),
            ).fetchone()
            if latest_attempt is not None and \
                    str(latest_attempt["status"] or "") == ATTEMPT_STATUS_UNKNOWN:
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
                    raise RuntimeConflictError(
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
            meta_json = json.dumps({"recovered_from_attempt_id": recovered_from},
                                   ensure_ascii=False) if recovered_from else "{}"
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
                SET current_attempt_id = ?, current_attempt_generation = ?, updated_at = ?
                WHERE agent_run_id = ? AND current_attempt_generation = ?
                """,
                (attempt_id, generation, now, agent_run_id, generation - 1),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"attempt CAS 失败: {agent_run_id} generation {generation - 1}->{generation}"
                )
            if old_attempt_id:
                # outcome_json 保留 claim 元数据（holder/lease/resource_scopes/
                # 幂等身份），takeover 原因并入 unknown_reason 键——整段覆盖会
                # 抹掉 resource_scopes，旧 settle 被拒后的锁释放/标 DIRTY 拿
                # 不到 scope（f2 sibling 撞锁）。
                stale_rows = conn.execute(
                    "SELECT operation_id, outcome_json FROM tool_operations "
                    "WHERE attempt_id = ? AND status IN ('CLAIMED', 'EXECUTING')",
                    (old_attempt_id,),
                ).fetchall()
                for stale in stale_rows:
                    payload = {}
                    try:
                        loaded = json.loads(stale["outcome_json"])
                    except (TypeError, json.JSONDecodeError):
                        loaded = {}
                    if isinstance(loaded, dict):
                        payload = dict(loaded)
                    payload["reason"] = "takeover_recovery"
                    payload["unknown_reason"] = "takeover_recovery"
                    conn.execute(
                        """
                        UPDATE tool_operations
                        SET status = 'UNKNOWN', outcome_json = ?, updated_at = ?
                        WHERE operation_id = ? AND status IN ('CLAIMED', 'EXECUTING')
                        """,
                        (json.dumps(payload, ensure_ascii=False), now, stale["operation_id"]),
                    )
                # seq 253 补：takeover 把旧 attempt 的非终态操作统一转 UNKNOWN
                # 后，其持有锁必须同事务释放——UNKNOWN 是终态（结果不可知、
                # worker 已不可信、不会再有人来 settle/删锁），锁残留会让同
                # scope 的任何后续 claim 撞 UNIQUE(canonical_scope) 死锁（f2
                # sibling），与 settle 终态删锁/_mark_unknown_single_transaction
                # 的「终态即释放」语义一致。
                conn.execute(
                    "DELETE FROM resource_locks WHERE attempt_id = ?",
                    (old_attempt_id,),
                )
                conn.execute(
                    """
                    UPDATE resource_mutations
                    SET state = 'DIRTY', dirty_reason = 'takeover_recovery', updated_at = ?
                    WHERE attempt_id = ? AND state != 'DIRTY'
                    """,
                    (now, old_attempt_id),
                )
            conn.commit()
        row = self.get_attempt(attempt_id)
        assert row is not None
        return row

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
        """R1-03 锁接管判定：过期或持主判死 → 可接管；无法证明 → 保守拒。"""
        if float(lock["lease_expires_at"] or 0) < now:
            return True
        return not holder_is_alive(int(lock["pid"] or 0),
                                   str(lock["start_token"] or ""))

    def has_active_exec_lock(self, agent_run_id: str, *, now: float | None = None) -> bool:
        """R1-03 驱动链 lease 感知：run 是否被活跃执行权锁持有。

        活跃 = 锁存在且不可接管（lease 未过期且持主存活）→ worker 在跑，
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
    def settle_agent_run(
        self,
        *,
        agent_run_id: str,
        status: str,
        payload: dict[str, Any] | None = None,
        now: float | None = None,
        attempt_id: str = "",
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
          - 终态即释放：收口成功同事务释放执行权锁（scope=attempt-exec:
            {agent_run_id}），锁不残留。
        """
        now = time.time() if now is None else now
        event_id = uuid.uuid4().hex
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
            # 2026-08-15 真机根因（3×3 cell2）：模型坏块整轮零执行留下的
            # 未启动 op（CLAIMED 且 handler 从未启动）在终态收口时如实落
            # CANCELLED（G.5：CANCELLED 只允许能证明 handler 未启动的操作；
            # coordinator 同款 not_started 语义）——不残留 UNKNOWN 残账。
            # 双席 seq1989 缺口4: outcome_json 必须 schema-valid——CANCELLED
            # 后重复 claim 进 terminal replay, 空 result 会被 _result_from_record
            # 降为 TOOL_OPERATION_OUTCOME_UNKNOWN; 写结构化取消结果
            # (schema_version/not_started/handler_executed=false/closeout 信息)
            # replay 才能如实读到「未启动已取消」而非降级 UNKNOWN。
            _unstarted_ops = conn.execute(
                "SELECT operation_id, operation_type FROM tool_operations "
                "WHERE agent_run_id = ? AND status = ? AND handler_started_at = 0",
                (agent_run_id, OP_CLAIMED),
            ).fetchall()
            for _op in _unstarted_ops:
                _cancel_result = json.dumps(
                    {
                        "schema": "managed_operation.v1",
                        "result": {
                            "schema_version": "tool_execution_result.v1",
                            "tool": str(_op["operation_type"] or ""),
                            "ok": False,
                            "output": (
                                "未启动(not_started): 整轮零执行, 操作从未执行, "
                                "无副作用(G.5 CANCELLED)"
                            ),
                            "error_code": "TOOL_OPERATION_CANCELLED_NOT_STARTED",
                            "effect_outcome": "not_started",
                            "handler_executed": False,
                        },
                        "error_code": "TOOL_OPERATION_CANCELLED_NOT_STARTED",
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    "UPDATE tool_operations SET status = ?, settled_at = ?, "
                    "outcome_json = ? WHERE operation_id = ?",
                    (OP_CANCELLED, now, _cancel_result, str(_op["operation_id"])),
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
        return {"settled": True, "event_id": event_id}

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
        """task_run 任务级终态化（发现层账本自愈用，无 acceptance 轴）。

        与 closeout_task_run 的区别：这是 run 已权威终态后把残留 task_run
        投影终态的兜底（孤儿回收/收口闸的 settle 调用点不在交付链，没资格
        走带 acceptance 轴的三轴收口）。closed_at=0 才写（CAS 幂等，并发/
        重放以第一次为准）；写 task_run.closed 事件，与 agent_run.completed
        对称可审计。
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

    # ------------------------------------------- R1-03 收口矩阵 + 孤儿兜底
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
        执行层收口 ≠ 任务层收口：任务完成仍走 required_actions +
        closeout_task_run + delivery 闸（本函数只裁决 attempt 执行痕迹）。
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
        2×lease）且 run status ∈ {''/created}——run 已终态的锁本应由 settle
        同事务释放，残留锁不构成孤儿（不重复收口已终态 run）。
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
            if float(lock["lease_expires_at"] or 0) >= now and \
                    holder_is_alive(int(lock["pid"] or 0),
                                    str(lock["start_token"] or "")):
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
                        (attempt_id, f"%\"reason\": \"side_effect_gate\"%"),
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
                    (attempt_id, f"%\"reason\": \"nonterminal_ops\"%"),
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
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT agent_run_id FROM agent_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return {"recovered": False, "reason": "no_such_attempt"}
            cur = conn.execute(
                "UPDATE agent_attempts SET status = ?, ended_at = ? "
                "WHERE attempt_id = ? AND status = ?",
                (ATTEMPT_STATUS_RECOVERED, now, attempt_id, ATTEMPT_STATUS_UNKNOWN),
            )
            if cur.rowcount != 1:
                return {"recovered": False, "reason": "not_unknown"}
            agent_run_id = str(row["agent_run_id"] or "")
            conn.execute(
                "DELETE FROM resource_locks WHERE canonical_scope = ?",
                (exec_lock_scope(agent_run_id),),
            )
            self._append_event_conn(
                conn,
                event_type="attempt_recovered",
                attempt_id=attempt_id,
                agent_run_id=agent_run_id,
                payload={
                    "status": ATTEMPT_STATUS_RECOVERED,
                    "operator": str(operator or ""),
                    "effect_disposition": effect_disposition,
                    "reason": str(reason or ""),
                },
            )
        return {"recovered": True, "attempt_id": attempt_id, "agent_run_id": agent_run_id}

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

    # ------------------------------------------------ continuation_handoffs
    # CLI 自动续跑显式移交(2026-08-14 长任务首要约束): 写入待消费移交单,
    # gateway 调度器 CAS 领取接管续跑。runtime.db 是 owner 级共享权威——
    # 不依赖进程 cwd 或入口私有 conversation store(CLI/gateway store 隔离
    # 真机坐实: 预算耗尽后 gateway 看不到 CLI 的 policy, 任务截断)。

    def create_continuation_handoff(
        self,
        *,
        agent_run_id: str,
        attempt_id: str,
        task_run_id: str = "",
        root_run_id: str = "",
        root_request_id: str = "",
        root_thread_id: str = "",
        root_task_id: str = "",
        user_prompt: str = "",
        continuation_seq: int = 0,
        reason: str = "",
        now: float | None = None,
    ) -> str:
        """写一条待接管移交单（同 agent_run+seq 幂等：已存在则跳过）。"""
        import uuid

        current = now if now is not None else time.time()
        existing = self.pending_continuation_handoffs(
            agent_run_id=agent_run_id, limit=10
        )
        if any(
            int(row.get("continuation_seq") or 0) == int(continuation_seq or 0)
            for row in existing
        ):
            return ""
        handoff_id = uuid.uuid4().hex
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO continuation_handoffs(
                    handoff_id, task_run_id, agent_run_id, attempt_id,
                    root_run_id, root_request_id, root_thread_id, root_task_id,
                    user_prompt, continuation_seq, reason, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    str(task_run_id or ""),
                    str(agent_run_id or ""),
                    str(attempt_id or ""),
                    str(root_run_id or ""),
                    str(root_request_id or ""),
                    str(root_thread_id or ""),
                    str(root_task_id or ""),
                    str(user_prompt or ""),
                    int(continuation_seq or 0),
                    str(reason or ""),
                    current,
                ),
            )
            conn.commit()
        return handoff_id

    def pending_continuation_handoffs(
        self,
        *,
        limit: int = 20,
        agent_run_id: str = "",
    ) -> list[dict[str, Any]]:
        """待接管移交单（consumed_at=0），最旧优先。"""
        with self._runtime_connection() as conn:
            if agent_run_id:
                rows = conn.execute(
                    "SELECT * FROM continuation_handoffs "
                    "WHERE consumed_at = 0 AND agent_run_id = ? "
                    "ORDER BY created_at ASC LIMIT ?",
                    (agent_run_id, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM continuation_handoffs "
                    "WHERE consumed_at = 0 "
                    "ORDER BY created_at ASC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [dict(row) for row in rows]

    def consume_continuation_handoff(
        self, handoff_id: str, *, consumed_by: str, now: float | None = None
    ) -> bool:
        """CAS 领取移交单（consumed_at=0 条件更新，防双消费）。"""
        current = now if now is not None else time.time()
        with self._runtime_connection() as conn:
            cursor = conn.execute(
                "UPDATE continuation_handoffs "
                "SET consumed_at = ?, consumed_by = ? "
                "WHERE handoff_id = ? AND consumed_at = 0",
                (current, str(consumed_by or ""), handoff_id),
            )
            conn.commit()
            return cursor.rowcount > 0

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

    # ------------------------------------------------ wake_intents（#233）
    # 叫醒意图单一权威状态机接口（规格 docs/design/WAKE_INTENT_SCHEDULING_SPEC.md）。
    # 状态：pending→claimed→handed_off + cancelled/expired；reconciliation 派生
    # 状态在 task/attempt ledger，intent 保持 claimed 直到显式恢复流程接管。
    # 硬不变量：仅 dispatcher claim 成功后创建 attempt；producer/queue/reconciler
    # 禁调模型禁建 attempt（调用方层保证，本层只提供 CAS 原语）。

    def create_wake_intent(
        self,
        *,
        intent_id: str,
        dedup_key: str,
        owner_id: str,
        source: str,
        wake_reason: str,
        next_wake_at: float,
        task_id: str = "",
        run_id: str = "",
        parent_run_id: str = "",
        root_run_id: str = "",
        execution_mode: str = "interactive",
        source_event_id: str = "",
        retry_event_id: str = "",
        provenance_ref: str = "",
        provider_scope_ref: str = "",
        continuation_policy: str = "",
        policy_generation: int = 0,
        priority: int = 0,
        not_before: float = 0,
        due_window: str = "",
        retry_after: float = 0,
        expires_at: float | None = None,
        idempotency_key: str = "",
        now: float | None = None,
    ) -> dict[str, object]:
        """producer 写 intent（dedup UNIQUE 冲突 → 幂等返回已存在）。

        dedup_key 由调用方（producer）按规格 §3 稳定计算
        （effective_source_event_id = retry_event_id or source_event_id），
        不含 attempt_id / provider key。UNIQUE 冲突 = 同一来源同一 due window
        已有 intent → 返回 existing=True，不覆盖。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT intent_id FROM wake_intents WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            if existing is not None:
                return {"created": False, "existing": True, "intent_id": existing["intent_id"]}
            conn.execute(
                "INSERT INTO wake_intents ("
                " intent_id, dedup_key, task_id, run_id, parent_run_id, root_run_id,"
                " owner_id, execution_mode, source, wake_reason, source_event_id,"
                " retry_event_id, provenance_ref, provider_scope_ref, continuation_policy,"
                " policy_generation, priority, not_before, next_wake_at, due_window,"
                " retry_after, expires_at, status, idempotency_key, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    intent_id, dedup_key, task_id, run_id, parent_run_id, root_run_id,
                    owner_id, execution_mode, source, wake_reason, source_event_id,
                    retry_event_id, provenance_ref, provider_scope_ref, continuation_policy,
                    policy_generation, priority, not_before, next_wake_at, due_window,
                    retry_after, expires_at, idempotency_key, now, now,
                ),
            )
        return {"created": True, "existing": False, "intent_id": intent_id}

    def due_wake_intents(
        self, *, limit: int = 50, now: float | None = None
    ) -> list[dict[str, Any]]:
        """due intent 查询（规格 §2 派生公式，persistent 索引 (status, next_wake_at)）。

        due = pending AND next_wake_at<=now AND (expires_at IS NULL OR now<expires_at)。
        policy 有效性（continuation_policy+policy_generation 匹配当前 ledger 且未撤销）
        由 dispatcher 层结合 policy ledger 裁决，本层只按表内到期事实过滤。
        """
        now = time.time() if now is None else now
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM wake_intents WHERE status = 'pending' "
                "AND next_wake_at <= ? AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY next_wake_at ASC LIMIT ?",
                (now, now, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_wake_intent(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        lease_seconds: float,
        claim_token: str,
        now: float | None = None,
    ) -> dict[str, object]:
        """CAS claim：pending → claimed（claim_generation+1，绑定 token/lease）。

        原子条件含规格前置（seq2425）：status=pending + due（next_wake_at<=now）
        + 未过期（expires_at 为空或 > now）。policy/来源授权/circuit 由
        dispatcher 上层裁决（见 owner_wake_discovery 来源断言）。claim_generation
        递增与 claim_token/lease_owner/lease_until 绑定，旧租约回收后不得凭旧
        generation 认领（规格 §2/§4 硬不变量 8）。返回实际 claim_generation
        作为 claim ledger 代际证据。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET status = 'claimed',"
                " claim_generation = claim_generation + 1, claim_token = ?,"
                " lease_owner = ?, lease_until = ?, claimed_at = ?, updated_at = ?"
                " WHERE intent_id = ? AND status = 'pending'"
                " AND next_wake_at <= ? AND (expires_at IS NULL OR expires_at > ?)"
                " AND active_attempt_id = ''",
                (
                    claim_token,
                    lease_owner,
                    now + max(1.0, lease_seconds),
                    now,
                    now,
                    intent_id,
                    now,
                    now,
                ),
            )
            if cur.rowcount == 0:
                return {"claimed": False, "reason": "not_pending_or_not_due"}
            row = conn.execute(
                "SELECT claim_generation FROM wake_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        generation = int(row[0]) if row is not None else None
        return {"claimed": True, "intent_id": intent_id, "claim_generation": generation}

    def release_wake_intent_lease(
        self,
        intent_id: str,
        *,
        claim_token: str,
        expected_generation: int,
        now: float | None = None,
    ) -> dict[str, object]:
        """lease 已过期（lease_until<=now）且已确认无已知副作用 → claimed 退回 pending。

        群复核门禁（seq2431/2433）：原子条件必须含 lease_until<=now 与
        claim_generation 匹配——租约未过期或代际不匹配一律拒绝（防活跃 lease
        被误释放 / 旧代际凭旧 token 操作）。未知副作用走
        mark_wake_intent_lease_expired（保持 claimed，不隐式重放）。

        seq2461 锁定：claim 后到 acceptance 前 lease 回收不得绕过 active gate
        ——**存在未完成的 dispatched dispatch ledger 记录时禁止 release**
        （等价阻止重认领）。dispatch 必须先行终态化（accepted/released/failed/
        reconciliation）后，release 才放行回 pending；accept 时已回填
        active_attempt_id 的 intent 由 active_attempt_id='' 条件天然拒绝。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET status = 'pending',"
                " claim_generation = claim_generation + 1,"
                " claim_token = '', lease_owner = '', lease_until = 0, updated_at = ?"
                " WHERE intent_id = ? AND status = 'claimed' AND claim_token = ?"
                " AND lease_until <= ? AND claim_generation = ?"
                " AND active_attempt_id = ''"
                " AND NOT EXISTS (SELECT 1 FROM wake_dispatches wd"
                "  WHERE wd.intent_id = wake_intents.intent_id"
                "  AND wd.status = 'dispatched')",
                (now, intent_id, claim_token, now, int(expected_generation)),
            )
            if cur.rowcount == 0:
                return {"released": False, "reason": "not_expired_or_token_or_generation_mismatch"}
        return {"released": True, "intent_id": intent_id}

    def mark_wake_intent_rejected(
        self, intent_id: str, *, error_ref: str, now: float | None = None
    ) -> dict[str, object]:
        """授权校验拒绝（#236）：pending 保持 + 标 last_error_ref（reconciler 审计）。

        与 lease-expired 不同：这是「不可绕过入口门」拒绝（policy/来源/scope
        无效），intent 保持 pending 但不被 dispatcher 消费——可被 reconciler
        审计或取消，绝不 claim 执行。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET last_error_ref = ?, updated_at = ?"
                " WHERE intent_id = ? AND status = 'pending'",
                (error_ref, now, intent_id),
            )
            if cur.rowcount == 0:
                return {"marked": False, "reason": "not_pending"}
        return {"marked": True, "intent_id": intent_id}

    def mark_wake_intent_lease_expired(
        self,
        intent_id: str,
        *,
        error_ref: str,
        claim_token: str,
        expected_generation: int,
        now: float | None = None,
    ) -> dict[str, object]:
        """lease 已过期（lease_until<=now）且副作用未知 → 保持 claimed + 标 last_error_ref。

        原子条件同 release（lease_until<=now + token + generation），防止租约
        未过期/旧代际误标。intent 不迁移（reconciliation_required 是
        task/attempt ledger 派生状态），等待显式恢复流程接管。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET last_error_ref = ?, updated_at = ?"
                " WHERE intent_id = ? AND status = 'claimed' AND claim_token = ?"
                " AND lease_until <= ? AND claim_generation = ?",
                (error_ref, now, intent_id, claim_token, now, int(expected_generation)),
            )
            if cur.rowcount == 0:
                return {"marked": False, "reason": "not_expired_or_token_or_generation_mismatch"}
        return {"marked": True, "intent_id": intent_id}

    def cancel_wake_intent(
        self, intent_id: str, *, reason: str, now: float | None = None
    ) -> dict[str, object]:
        """取消：pending/claimed → cancelled（仅未 handed_off 可取消）。"""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET status = 'cancelled', cancelled_reason = ?,"
                " finished_at = ?, updated_at = ?"
                " WHERE intent_id = ? AND status IN ('pending', 'claimed')",
                (reason, now, now, intent_id),
            )
            if cur.rowcount == 0:
                return {"cancelled": False, "reason": "not_cancellable"}
        return {"cancelled": True, "intent_id": intent_id}

    def expire_stale_wake_intents(self, *, grace_seconds: float, now: float | None = None) -> int:
        """pending 超宽限（next_wake_at + grace）未被 claim → expired（进 reconciler 审计）。"""
        now = time.time() if now is None else now
        cutoff = now - max(0.0, grace_seconds)
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET status = 'expired', finished_at = ?, updated_at = ?"
                " WHERE status = 'pending' AND next_wake_at < ?",
                (now, now, cutoff),
            )
            return int(cur.rowcount or 0)

    def get_wake_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT * FROM wake_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def stale_claimed_wake_intents(
        self, *, limit: int = 20, now: float | None = None
    ) -> list[dict[str, Any]]:
        """claimed + 租约已过期（lease_until<=now）的 intent（bounded reaper 输入）。

        群定稿（seq2481/2484①）：回收执行者每轮按此查询限量处理——无 dispatch/
        无 attempt/无已知副作用 → CAS release 回 pending；有 attempt 或副作用
        未知 → mark reconciliation，禁止自动重放。租约未过期的一律不碰。
        """
        now = time.time() if now is None else now
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM wake_intents WHERE status = 'claimed' AND lease_until <= ?"
                " ORDER BY lease_until ASC LIMIT ?",
                (now, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def wake_intent_status_counts(self) -> dict[str, int]:
        """状态计数（审计/验收：525 零调度等证据来源）。"""
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT status, count(*) AS n FROM wake_intents GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    # -------------------------------------------------- wake_policies（#233）
    def set_wake_policy(
        self,
        *,
        policy_id: str,
        owner_id: str,
        continuation_policy: str,
        policy_generation: int,
        allowed_sources: str = "",
        provider_scope_ref: str = "",
        scope_ref: str = "",
        now: float | None = None,
    ) -> dict[str, object]:
        """写入/更新 canonical 策略（seq2458 合同：generation 更新与 revoked_at
        变更同事务原子化——旧代次先置 revoked，再落新行）。

        seq2463①：**保留撤销历史**——不用 INSERT OR REPLACE（那会删旧行），
        改普通 INSERT：先 UPDATE 未撤销旧行置 revoked_at，再 INSERT 新行。
        partial unique index（revoked_at=0 才唯一）保证同 owner+policy 至多一条
        未撤销行；历史行（已撤销）保留可审计。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            conn.execute(
                "UPDATE wake_policies SET revoked_at = ?, updated_at = ?"
                " WHERE owner_id = ? AND continuation_policy = ? AND revoked_at = 0",
                (now, now, owner_id, continuation_policy),
            )
            conn.execute(
                "INSERT INTO wake_policies ("
                " policy_id, owner_id, scope_ref, continuation_policy,"
                " policy_generation, allowed_sources, provider_scope_ref,"
                " revoked_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    policy_id, owner_id, scope_ref, continuation_policy,
                    int(policy_generation), allowed_sources, provider_scope_ref, now,
                ),
            )
        return {"set": True, "policy_id": policy_id}

    def revoke_wake_policy(
        self,
        *,
        owner_id: str,
        continuation_policy: str,
        now: float | None = None,
    ) -> dict[str, object]:
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_policies SET revoked_at = ?, updated_at = ?"
                " WHERE owner_id = ? AND continuation_policy = ? AND revoked_at = 0",
                (now, now, owner_id, continuation_policy),
            )
            if cur.rowcount == 0:
                return {"revoked": False, "reason": "not_current"}
        return {"revoked": True}

    def current_wake_policy(
        self, owner_id: str, continuation_policy: str
    ) -> dict[str, Any] | None:
        """按 owner+continuation_policy 选唯一未撤销 generation（seq2461 锁定）。

        读取规则（唯一索引本身不能替代）：UNIQUE(owner_id, continuation_policy)
        保证同 owner+policy 至多一行；本方法再以 `revoked_at=0` 过滤 +
        `ORDER BY updated_at DESC LIMIT 1` 防御性收敛——**只返回未撤销的当前
        代次**。旧代撤销（set_wake_policy 同事务置 revoked）后本方法即返回 None
        → wake_policy_allows fail-closed（无「两套当前策略」并存）。调用方用
        返回行继续校验 scope/allowed_sources/provider_scope_ref（见
        wake_policy_allows）。
        """
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT * FROM wake_policies"
                " WHERE owner_id = ? AND continuation_policy = ? AND revoked_at = 0"
                " ORDER BY updated_at DESC LIMIT 1",
                (owner_id, continuation_policy),
            ).fetchone()
        return dict(row) if row is not None else None

    def wake_policy_allows(self, row: dict[str, Any]) -> bool:
        """canonical 策略授权裁决（seq2455/2457/2463 硬合同，唯一授权源）。

        fail-closed 全链（任一不满足 → False）：
          - 无当前未撤销策略行 / 代际不匹配 → 拒
          - allowed_sources 为空 → 拒（白名单缺失不隐式放大授权，不隐式 wildcard）
          - provider_scope_ref 为空 → 拒
          - source 不在白名单 → 拒
          - provider scope 与 intent 不匹配 → 拒
          - scope_ref 为空 → 拒；非空 → 与 intent 的 owner/task/run 派生作用域
            做前缀匹配（owner[:task[:run]]），owner 级策略不得越过作用域。
        只读 intent 行结构化字段，不解析自然语言。
        """
        owner_id = str(row.get("owner_id") or "")
        continuation_policy = str(row.get("continuation_policy") or "").strip()
        try:
            policy_generation = int(row.get("policy_generation") or -1)
        except (TypeError, ValueError):
            return False
        source = str(row.get("source") or "").strip()
        provider_scope_ref = str(row.get("provider_scope_ref") or "").strip()
        task_id = str(row.get("task_id") or "").strip()
        run_id = str(row.get("run_id") or "").strip()
        policy = self.current_wake_policy(owner_id, continuation_policy)
        if policy is None:
            return False
        if int(policy["revoked_at"] or 0) != 0:
            return False
        if int(policy["policy_generation"]) != policy_generation:
            return False
        allowed = {s.strip() for s in str(policy["allowed_sources"] or "").split(",") if s.strip()}
        if not allowed:
            return False  # 空白名单 fail-closed（seq2463②：不隐式 wildcard）
        if source not in allowed:
            return False
        policy_scope = str(policy["provider_scope_ref"] or "").strip()
        if not policy_scope:
            return False  # 空 provider scope fail-closed
        if policy_scope != provider_scope_ref:
            return False
        scope_ref = str(policy["scope_ref"] or "").strip()
        if not scope_ref:
            return False  # 空作用域 fail-closed（seq2463②：owner 级策略不得越过作用域）
        if not self._policy_scope_matches(scope_ref, owner_id, task_id, run_id):
            return False
        return True

    @staticmethod
    def _policy_scope_matches(scope_ref: str, owner_id: str, task_id: str, run_id: str) -> bool:
        """policy.scope_ref 必须是被授权作用域的段前缀（* 为显式通配段）。

        scope_ref="owner" → 该 owner 全部；"owner:task" → 该 task；"owner:task:run"
        → 该 run。更窄的 policy 不能授权更宽的 intent（防止 owner 级策略越过
        作用域）。显式 wildcard "*" 字段受审计（写策略时留痕）。
        """
        if scope_ref == "*":
            return True
        parts = [p for p in scope_ref.split(":") if p]
        if not parts:
            return False
        effective = [owner_id]
        if task_id:
            effective.append(task_id)
        if run_id:
            effective.append(run_id)
        if len(parts) > len(effective):
            return False
        return all(p == e or p == "*" for p, e in zip(parts, effective))

    # ------------------------------------------------- provider_circuits（#233）
    def freeze_provider_circuit(
        self,
        provider_scope_ref: str,
        *,
        retry_after: float,
        reason: str,
        now: float | None = None,
    ) -> dict[str, object]:
        """429/quota 冻结 provider（可审计来源 reason）。"""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO provider_circuits ("
                " provider_scope_ref, frozen, retry_after, reason, updated_at"
                ") VALUES (?, 1, ?, ?, ?)",
                (provider_scope_ref, retry_after, reason, now),
            )
        return {"frozen": True, "provider_scope_ref": provider_scope_ref}

    def recover_provider_circuit(
        self, provider_scope_ref: str, *, now: float | None = None
    ) -> dict[str, object]:
        now = time.time() if now is None else now
        with self.transaction() as conn:
            conn.execute(
                "UPDATE provider_circuits SET frozen = 0, retry_after = 0,"
                " reason = '', updated_at = ? WHERE provider_scope_ref = ?",
                (now, provider_scope_ref),
            )
        return {"recovered": True, "provider_scope_ref": provider_scope_ref}

    def provider_circuit_frozen(self, provider_scope_ref: str, *, now: float | None = None) -> bool:
        """circuit open（冻结且未过 retry_after）→ True，dispatcher 拒消费。"""
        now = time.time() if now is None else now
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT frozen, retry_after FROM provider_circuits"
                " WHERE provider_scope_ref = ?",
                (provider_scope_ref,),
            ).fetchone()
        if row is None:
            return False
        return int(row["frozen"] or 0) == 1 and float(row["retry_after"] or 0) > now

    # ------------------------------------------------ wake_dispatches（#233）
    def claim_and_record_wake_dispatch(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        lease_seconds: float,
        claim_token: str,
        dispatch_event_id: str,
        handoff_id: str,
        now: float | None = None,
    ) -> dict[str, object]:
        """claim + 落 dispatch outbox 同事务（同一 runtime.db，seq2458 合同①/③）。

        - intent pending→claimed（CAS 含 active_attempt_id='' 门：同 intent 同时
          最多一个 active attempt）。
        - 同一事务 INSERT wake_dispatches（status=dispatched、唯一
          dispatch_event_id + 唯一 handoff_id）——崩溃后 outbox 行已持久，可重放。
        内存 registry.record 在 dispatcher 上层仍执行（候选入池），但只有
        accept_wake_dispatch 才把 intent 置 handed_off（执行席可靠接收）。
        """
        now = time.time() if now is None else now
        dispatch_id = f"wake-dispatch-{int(now * 1000)}-{uuid.uuid4().hex[:12]}"
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET status = 'claimed',"
                " claim_generation = claim_generation + 1, claim_token = ?,"
                " lease_owner = ?, lease_until = ?, claimed_at = ?, updated_at = ?"
                " WHERE intent_id = ? AND status = 'pending'"
                " AND next_wake_at <= ? AND (expires_at IS NULL OR expires_at > ?)"
                " AND active_attempt_id = ''",
                (
                    claim_token,
                    lease_owner,
                    now + max(1.0, lease_seconds),
                    now,
                    now,
                    intent_id,
                    now,
                    now,
                ),
            )
            if cur.rowcount == 0:
                return {"claimed": False, "reason": "not_pending_or_not_due"}
            row = conn.execute(
                "SELECT claim_generation FROM wake_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            generation = int(row[0]) if row is not None else None
            conn.execute(
                "INSERT INTO wake_dispatches ("
                " dispatch_id, intent_id, claim_generation, claim_token, owner_id,"
                " lease_owner, lease_until, dispatch_event_id, handoff_id, attempt_id,"
                " status, created_at, updated_at"
                ") SELECT ?, ?, ?, ?, owner_id, ?, ?, ?, ?, '', 'dispatched', ?, ?"
                " FROM wake_intents WHERE intent_id = ?",
                (
                    dispatch_id, intent_id, generation, claim_token, lease_owner,
                    now + max(1.0, lease_seconds), dispatch_event_id, handoff_id,
                    now, now, intent_id,
                ),
            )
        return {
            "claimed": True,
            "intent_id": intent_id,
            "claim_generation": generation,
            "claim_token": claim_token,
            "dispatch_id": dispatch_id,
            "dispatch_event_id": dispatch_event_id,
            "handoff_id": handoff_id,
        }

    def get_wake_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT * FROM wake_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def wake_dispatches_for_intent(self, intent_id: str) -> list[dict[str, Any]]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM wake_dispatches WHERE intent_id = ?"
                " ORDER BY created_at ASC",
                (intent_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def accept_wake_dispatch(
        self,
        dispatch_id: str,
        *,
        handoff_id: str,
        attempt_id: str,
        now: float | None = None,
    ) -> dict[str, object]:
        """执行席可靠接收后 CAS：dispatch dispatched→accepted + intent
        claimed→handed_off（同一 runtime.db 单事务，seq2458 合同①②）。

        前置硬门（全部满足才 CAS）：
          - dispatch 行存在且 status='dispatched'（或已 accepted 且 handoff/attempt
            一致 → 幂等返回成功，重复投递安全）。
          - dispatch.handoff_id == 传入 handoff_id（唯一 handoff 事件）。
          - intent status='claimed' AND claim_generation/token 与 dispatch 一致。
          - **当前 lease 未过期（lease_until > now）**——lease 已过期的迟到
            receipt → 拒绝并转 reconciliation，不得把旧 intent 标 handed_off。
        成功后 intent.active_attempt_id=attempt_id（intent 级 active 门），
        provider/模型副作用才允许开始（acceptance 后）。
        """
        now = time.time() if now is None else now
        attempt_id = str(attempt_id or "").strip()
        if not attempt_id:
            return {"accepted": False, "reason": "attempt_id_required"}
        with self.transaction() as conn:
            # seq2463③：attempt_id 必须是 canonical agent_attempts 的受控 ref
            # （执行席创建 attempt 后回传），不存在则拒绝——防任意字符串抢占。
            attempt_exists = conn.execute(
                "SELECT 1 FROM agent_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if attempt_exists is None:
                return {"accepted": False, "reason": "attempt_not_found"}
            d = conn.execute(
                "SELECT * FROM wake_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            if d is None:
                return {"accepted": False, "reason": "dispatch_not_found"}
            # seq2466②：attempt 必须归属于本 dispatch 的 intent（owner/task/run
            # 受控关联）——不能把同库任意现存 attempt 绑到该 dispatch。
            intent_row = conn.execute(
                "SELECT owner_id, task_id, run_id FROM wake_intents WHERE intent_id = ?",
                (d["intent_id"],),
            ).fetchone()
            if intent_row is None:
                return {"accepted": False, "reason": "intent_not_found"}
            atr = self._attempt_owner_task_run(conn, attempt_id)
            if atr is None:
                return {"accepted": False, "reason": "attempt_chain_not_found"}
            attempt_owner, attempt_task, attempt_run = atr
            if attempt_owner != str(intent_row["owner_id"] or ""):
                return {"accepted": False, "reason": "attempt_owner_mismatch"}
            intent_task = str(intent_row["task_id"] or "").strip()
            if intent_task and intent_task != attempt_task:
                return {"accepted": False, "reason": "attempt_task_mismatch"}
            intent_run = str(intent_row["run_id"] or "").strip()
            if intent_run and intent_run != attempt_run:
                return {"accepted": False, "reason": "attempt_run_mismatch"}
            status = str(d["status"] or "")
            if status == "accepted":
                if str(d["handoff_id"] or "") == handoff_id and \
                        str(d["attempt_id"] or "") == attempt_id:
                    return {"accepted": True, "idempotent": True, "intent_id": d["intent_id"]}
                return {"accepted": False, "reason": "already_accepted_mismatch"}
            if status != "dispatched":
                return {"accepted": False, "reason": f"not_dispatched:{status}"}
            if str(d["handoff_id"] or "") != handoff_id:
                return {"accepted": False, "reason": "handoff_mismatch"}
            if float(d["lease_until"] or 0) <= now:
                conn.execute(
                    "UPDATE wake_dispatches SET status = 'reconciliation',"
                    " reconciliation_ref = 'late_receipt_lease_expired',"
                    " last_error_ref = 'late_receipt_lease_expired', updated_at = ?"
                    " WHERE dispatch_id = ? AND status = 'dispatched'",
                    (now, dispatch_id),
                )
                return {"accepted": False, "reason": "lease_expired_reconciliation"}
            cur = conn.execute(
                "UPDATE wake_intents SET status = 'handed_off', handoff_id = ?,"
                " handed_off_at = ?, active_attempt_id = ?, updated_at = ?"
                " WHERE intent_id = ? AND status = 'claimed'"
                " AND claim_generation = ? AND claim_token = ?",
                (
                    handoff_id, now, attempt_id, now,
                    d["intent_id"], int(d["claim_generation"]), d["claim_token"],
                ),
            )
            if cur.rowcount == 0:
                return {"accepted": False, "reason": "intent_not_claimed_generation_mismatch"}
            conn.execute(
                "UPDATE wake_dispatches SET status = 'accepted', handoff_id = ?,"
                " attempt_id = ?, accepted_at = ?, updated_at = ?"
                " WHERE dispatch_id = ? AND status = 'dispatched'",
                (handoff_id, attempt_id, now, now, dispatch_id),
            )
        return {"accepted": True, "idempotent": False, "intent_id": d["intent_id"]}

    def pending_dispatch_for_scope(
        self, owner_id: str, task_id: str, run_id: str = "", *, now: float | None = None
    ) -> dict[str, Any] | None:
        """owner/task/run 作用域下「已 claim 但未 accept」的 dispatch（acceptance 接线输入）。

        #233-3/4：执行席（owner scheduler）建 attempt 前查询——返回关联 intent
        status='claimed' 且 dispatch status='dispatched'、lease 未过期的行
        （含 dispatch_id/handoff_id），供 _bind_main_agent_authority 建 attempt 后
        accept_wake_dispatch 幂等确认。无 pending dispatch → None（普通 run 不
        触碰 wake 状态机）。只读查询，不做任何状态迁移。
        """
        now = time.time() if now is None else now
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT wd.dispatch_id, wd.handoff_id, wd.intent_id"
                " FROM wake_dispatches wd"
                " JOIN wake_intents wi ON wi.intent_id = wd.intent_id"
                " WHERE wi.owner_id = ? AND wi.task_id = ?"
                " AND (? = '' OR wi.run_id = ?)"
                " AND wi.status = 'claimed' AND wd.status = 'dispatched'"
                " AND wd.lease_until > ?"
                " ORDER BY wd.created_at ASC LIMIT 1",
                (owner_id, task_id, run_id, run_id, now),
            ).fetchone()
        return dict(row) if row is not None else None

    def has_unfinished_attempt_for_scope(
        self, owner_id: str, task_id: str, run_id: str = ""
    ) -> bool:
        """owner/task/run 作用域存在未完成 canonical attempt（ended_at=0）。

        reaper attempt 关联窗口检查（seq2490①/2492②）：dispatch.attempt_id 空
        不代表从未建 attempt——「canonical attempt 已落但 acceptance 未回写」
        窗口内 intent 不得释放（否则孤儿 attempt + 重派）。按
        agent_attempts→agent_runs→task_runs→tasks 权威链过滤 owner/task/run。
        """
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM agent_attempts aa"
                " JOIN agent_runs ar ON ar.agent_run_id = aa.agent_run_id"
                " JOIN task_runs tr ON tr.task_run_id = ar.task_run_id"
                " JOIN tasks t ON t.task_id = tr.task_id"
                " WHERE aa.ended_at = 0 AND t.owner_id = ? AND tr.task_id = ?"
                " AND (? = '' OR ar.run_id = ?) LIMIT 1",
                (owner_id, task_id, run_id, run_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _attempt_owner_task_run(conn: sqlite3.Connection, attempt_id: str) -> tuple[str, str, str] | None:
        """attempt → agent_runs → task_runs → tasks 链取 (owner_id, task_id, run_id)。

        seq2466②：accept 时校验 attempt 归属于 intent 的 owner/task/run——
        不能只验证 ID 存在。查不到权威链 → None（拒绝）。
        """
        row = conn.execute(
            "SELECT t.owner_id, tr.task_id, ar.run_id"
            " FROM agent_attempts aa"
            " JOIN agent_runs ar ON ar.agent_run_id = aa.agent_run_id"
            " JOIN task_runs tr ON tr.task_run_id = ar.task_run_id"
            " JOIN tasks t ON t.task_id = tr.task_id"
            " WHERE aa.attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return (str(row["owner_id"] or ""), str(row["task_id"] or ""), str(row["run_id"] or ""))

    def _dispatch_owned_by(self, conn, dispatch_id: str, *, claim_token: str,
                           expected_generation: int, lease_owner: str) -> dict | None:
        """终结 CAS 受控调用方身份校验（seq2463⑤）：dispatch 行必须匹配
        调用方持有的 claim_token / claim_generation / lease_owner，否则视为
        终结别人的 dispatch → 拒绝。返回 dispatch 行 dict 或 None。"""
        d = conn.execute(
            "SELECT * FROM wake_dispatches WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()
        if d is None:
            return None
        if str(d["claim_token"] or "") != claim_token:
            return None
        if int(d["claim_generation"] or -1) != int(expected_generation):
            return None
        if str(d["lease_owner"] or "") != lease_owner:
            return None
        return dict(d)

    def fail_wake_dispatch(
        self, dispatch_id: str, *, error_ref: str, claim_token: str,
        expected_generation: int, lease_owner: str, now: float | None = None,
    ) -> dict[str, object]:
        """执行席拒绝/接收失败：dispatch→failed（intent 保持 claimed，等 reconciler）。

        seq2463⑤：终结 CAS 校验 claim_token/generation/lease_owner 配对——任意
        持有事件 ID 的路径不能终结别人的 dispatch。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            if self._dispatch_owned_by(conn, dispatch_id, claim_token=claim_token,
                                       expected_generation=expected_generation,
                                       lease_owner=lease_owner) is None:
                return {"failed": False, "reason": "not_owned_or_not_dispatched"}
            cur = conn.execute(
                "UPDATE wake_dispatches SET status = 'failed',"
                " last_error_ref = ?, updated_at = ?"
                " WHERE dispatch_id = ? AND status = 'dispatched'",
                (error_ref, now, dispatch_id),
            )
            if cur.rowcount == 0:
                return {"failed": False, "reason": "not_dispatched"}
        return {"failed": True}

    def release_wake_dispatch(
        self, dispatch_id: str, *, claim_token: str,
        expected_generation: int, lease_owner: str, now: float | None = None,
    ) -> dict[str, object]:
        """无已知副作用 + lease 回收：dispatch→released（与 release_wake_intent_lease 配对）。

        seq2463⑤：终结 CAS 校验 claim_token/generation/lease_owner 配对。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            if self._dispatch_owned_by(conn, dispatch_id, claim_token=claim_token,
                                       expected_generation=expected_generation,
                                       lease_owner=lease_owner) is None:
                return {"released": False, "reason": "not_owned_or_not_dispatched"}
            cur = conn.execute(
                "UPDATE wake_dispatches SET status = 'released', updated_at = ?"
                " WHERE dispatch_id = ? AND status = 'dispatched'",
                (now, dispatch_id),
            )
            if cur.rowcount == 0:
                return {"released": False, "reason": "not_dispatched"}
        return {"released": True}

    def release_wake_intent_and_dispatch(
        self,
        intent_id: str,
        *,
        dispatch_id: str,
        claim_token: str,
        expected_generation: int,
        lease_owner: str,
        now: float | None = None,
    ) -> dict[str, object]:
        """原子回收：dispatch 终态化 + intent 回 pending 同一事务（seq2492③）。

        群复核硬门（2492③/2493①）：两步各自事务之间有崩溃窗口（dispatch 已
        released 而 intent 仍 claimed → 永久卡单）——本方法在同一事务内：
          - 重校验 lease_until<=now + claim_token/generation/lease_owner（
            lease 下沉，不依赖调用顺序）
          - dispatch 行 lease 字段与 intent 一致
          - dispatch dispatched→released + intent claimed→pending 原子完成
        任一校验失败 → 整体回滚（不留下半完成状态）。
        """
        now = time.time() if now is None else now
        with self.transaction() as conn:
            intent_row = conn.execute(
                "SELECT * FROM wake_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if intent_row is None:
                return {"released": False, "reason": "intent_not_found"}
            if str(intent_row["status"] or "") != "claimed":
                return {"released": False, "reason": "intent_not_claimed"}
            if str(intent_row["claim_token"] or "") != claim_token:
                return {"released": False, "reason": "claim_token_mismatch"}
            if int(intent_row["claim_generation"] or -1) != int(expected_generation):
                return {"released": False, "reason": "claim_generation_mismatch"}
            if str(intent_row["lease_owner"] or "") != lease_owner:
                return {"released": False, "reason": "lease_owner_mismatch"}
            if float(intent_row["lease_until"] or 0) > now:
                return {"released": False, "reason": "lease_not_expired"}
            d = conn.execute(
                "SELECT * FROM wake_dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            if d is None:
                return {"released": False, "reason": "dispatch_not_found"}
            if str(d["status"] or "") != "dispatched":
                return {"released": False, "reason": f"not_dispatched:{d['status']}"}
            # dispatch/intent lease 字段一致（同一 claim 的证据）
            if float(d["lease_until"] or 0) != float(intent_row["lease_until"] or 0):
                return {"released": False, "reason": "dispatch_intent_lease_mismatch"}
            if str(d["claim_token"] or "") != claim_token:
                return {"released": False, "reason": "dispatch_token_mismatch"}
            # 同事务原子：dispatch 终态化 + intent 回 pending
            conn.execute(
                "UPDATE wake_dispatches SET status = 'released', updated_at = ?"
                " WHERE dispatch_id = ? AND status = 'dispatched'",
                (now, dispatch_id),
            )
            conn.execute(
                "UPDATE wake_intents SET status = 'pending',"
                " claim_generation = claim_generation + 1,"
                " claim_token = '', lease_owner = '', lease_until = 0, updated_at = ?"
                " WHERE intent_id = ? AND status = 'claimed' AND claim_token = ?"
                " AND lease_until <= ? AND claim_generation = ?"
                " AND active_attempt_id = ''",
                (now, intent_id, claim_token, now, int(expected_generation)),
            )
        return {"released": True, "intent_id": intent_id}

    def mark_wake_dispatch_reconciliation(
        self, dispatch_id: str, *, error_ref: str, claim_token: str,
        expected_generation: int, lease_owner: str, now: float | None = None,
    ) -> dict[str, object]:
        """dispatch→reconciliation（seq2463⑤：终结 CAS 校验调用方身份配对）。"""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            if self._dispatch_owned_by(conn, dispatch_id, claim_token=claim_token,
                                       expected_generation=expected_generation,
                                       lease_owner=lease_owner) is None:
                return {"marked": False, "reason": "not_owned_or_not_markable"}
            cur = conn.execute(
                "UPDATE wake_dispatches SET status = 'reconciliation',"
                " last_error_ref = ?, reconciliation_ref = ?, updated_at = ?"
                " WHERE dispatch_id = ? AND status IN ('dispatched', 'failed')",
                (error_ref, error_ref, now, dispatch_id),
            )
            if cur.rowcount == 0:
                return {"marked": False, "reason": "not_markable"}
        return {"marked": True}

    def clear_wake_intent_active_attempt(
        self, intent_id: str, *, attempt_id: str, now: float | None = None
    ) -> dict[str, object]:
        """attempt 终态时清除 intent 级 active 门（CAS 按 attempt_id，防误清新代次）。"""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE wake_intents SET active_attempt_id = '', updated_at = ?"
                " WHERE intent_id = ? AND active_attempt_id = ?",
                (now, intent_id, attempt_id),
            )
            if cur.rowcount == 0:
                return {"cleared": False, "reason": "not_matching_attempt"}
        return {"cleared": True}


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
