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

from ..common.id_generator import new_id
from .acceptance_operations import RuntimeAcceptanceMixin
from .delivery_operations import RuntimeDeliveryMixin
from .operations import RuntimeConflictError, RuntimeOperationsMixin
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

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
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

    # ---------------------------------------------------------- AgentAttempt
    def create_attempt(self, agent_run_id: str) -> sqlite3.Row:
        """CAS 创建新 attempt 并替换 current pointer（F.2/F.3）。

        读当前 generation → 新 generation = +1 → UPDATE agent_runs 时以
        current_attempt_generation 为 CAS 条件；0 行命中 = 并发冲突，fail-closed。
        旧 attempt 行不可变（F.7）。

        G4 补（3.txt G4-4）：takeover 原子收尾并入同一事务——旧 attempt 的
        非终态 operation（CLAIMED/EXECUTING）统一转 UNKNOWN（无法证明零
        副作用，绝不让旧 worker 事后写 SUCCEEDED——settle 单事务 fence
        已拒，此处由系统权威闭环），其 workspace mutation 全部标 DIRTY
        （G.14：reconcile 前阻止发布/验收/交付）。旧 worker 即使还活着
        也只能看到 UNKNOWN/DIRTY，无法盲重放。
        """
        now = time.time()
        with self._runtime_connection() as conn:
            run = conn.execute(
                "SELECT current_attempt_generation, current_attempt_id "
                "FROM agent_runs WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"agent_run 不存在: {agent_run_id}")
            generation = int(run["current_attempt_generation"]) + 1
            old_attempt_id = str(run["current_attempt_id"] or "")
            attempt_id = new_id("attempt_id")
            conn.execute(
                """
                INSERT INTO agent_attempts(attempt_id, agent_run_id, attempt_generation,
                                           status, started_at)
                VALUES(?, ?, ?, 'running', ?)
                """,
                (attempt_id, agent_run_id, generation, now),
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
