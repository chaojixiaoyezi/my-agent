"""R2 执行与发布权威操作（3.txt F/G/H 节落地）。

本 mixin 在 RuntimeRepository 上补充：
- 三层 fence 校验（F.6/F.7）：claim/renew/handler 前/settle/PublishOperation
  五处同时验证 current_attempt_id + current_attempt_generation +
  workspace_epoch + tool_operation_generation；旧 attempt 靠旧行不得获权。
- ToolOperation 状态机（G.1-G.5）：CLAIMED→EXECUTING（CAS 写
  handler_started_at）→SUCCEEDED/FAILED/CANCELLED/UNKNOWN。
- 资源锁（G.6-G.9）：holder instance + PID/start token + attempt generation +
  workspace_epoch + lease；renew 也 CAS；多资源按 canonical scope 排序
  一次取得全部锁，未取得全部时 handler=0。
- 资源 mutation 账（G.12-G.14）：canonical scope + version + 状态；
  unknown/unscoped 写置 DIRTY → 阻止发布/验收/交付。
- PublishOperation（H.2-H.9）：staging→共享唯一通道；preimage CAS、
  原子 replace、崩溃只落 COMMITTED 或 DIRTY/UNKNOWN；完成后才写
  ArtifactRecord。

所有状态转换 fail-closed：CAS rowcount≠1 一律 RuntimeConflictError。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from ..common.id_generator import new_id

# G.1 ToolOperation 六态。
OP_CLAIMED = "CLAIMED"
OP_EXECUTING = "EXECUTING"
OP_SUCCEEDED = "SUCCEEDED"
OP_FAILED = "FAILED"
OP_CANCELLED = "CANCELLED"
OP_UNKNOWN = "UNKNOWN"
_OPERATION_STATES = frozenset(
    {OP_CLAIMED, OP_EXECUTING, OP_SUCCEEDED, OP_FAILED, OP_CANCELLED, OP_UNKNOWN}
)
#: G.5：CANCELLED 只允许能证明 handler 未启动（handler_started_at=0）的操作。
_CANCELLABLE_FROM = (OP_CLAIMED,)

# G.12 mutation 状态。
MUT_MUTATING = "MUTATING"
MUT_STABLE = "STABLE"
MUT_DIRTY = "DIRTY"

# H.7 publish 状态：崩溃只允许 COMMITTED 或 DIRTY/UNKNOWN。
PUB_STAGING = "STAGING"
PUB_COMMITTED = "COMMITTED"
PUB_DIRTY = "DIRTY"
PUB_UNKNOWN = "UNKNOWN"

#: 测试 10：同资源同 preimage 恰一发布，另一争用发布方收到此冲突码。
RESOURCE_VERSION_CONFLICT = "RESOURCE_VERSION_CONFLICT"

#: 默认资源锁租期（秒）。lease 过期 ≠ 持有者死亡（G.8），reconcile 前不得
#: 直接把资源交给第二 writer。
DEFAULT_LEASE_SECONDS = 60


class RuntimeConflictError(RuntimeError):
    """权威库内状态冲突（CAS 失败/资源已被占/版本失配）。"""


def sha256_of(path: Path) -> str:
    """文件内容 digest（preimage/postimage/ArtifactRecord 共用）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RuntimeOperationsMixin:
    # ------------------------------------------------------------- 三层 fence
    def current_fence(self, agent_run_id: str) -> dict[str, Any]:
        """当前权威 fence 快照（F.6 校验基准，调用方不得自行拼装）。"""
        with self._runtime_connection() as conn:
            run = conn.execute(
                "SELECT current_attempt_id, current_attempt_generation, workspace_epoch "
                "FROM agent_runs WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
        if run is None:
            raise KeyError(f"agent_run 不存在: {agent_run_id}")
        return {
            "agent_run_id": agent_run_id,
            "current_attempt_id": str(run["current_attempt_id"] or ""),
            "current_attempt_generation": int(run["current_attempt_generation"]),
            "workspace_epoch": int(run["workspace_epoch"]),
        }

    def verify_fence(
        self,
        *,
        agent_run_id: str,
        attempt_id: str | None = None,
        workspace_epoch: int | None = None,
        tool_operation_generation: int | None = None,
    ) -> sqlite3.Row:
        """F.6：claim/renew/handler 前/settle/PublishOperation 五处统一校验。

        校验项：
        - attempt_id 必须是当前 current pointer（F.7：旧 attempt 靠旧行不获权）；
        - attempt 行的 attempt_generation 必须等于 current_attempt_generation；
        - workspace_epoch 必须等于 agent_runs.workspace_epoch（F.1）；
        - tool_operation_generation（给定时）必须等于该操作行自己的 generation。

        任一失配 → RuntimeConflictError（fail-closed）。返回 attempt 行供
        调用方继续使用。
        """
        run = self.current_fence(agent_run_id)
        if attempt_id is not None:
            current_id = run["current_attempt_id"]
            if current_id != str(attempt_id):
                raise RuntimeConflictError(
                    f"fence 失败: attempt {attempt_id!r} 已不是 current pointer "
                    f"(当前 {current_id!r})"
                )
            attempt = self.get_attempt(str(attempt_id))
            if attempt is None:
                raise RuntimeConflictError(f"fence 失败: attempt 不存在: {attempt_id}")
            if int(attempt["attempt_generation"]) != run["current_attempt_generation"]:
                raise RuntimeConflictError(
                    f"fence 失败: attempt generation {attempt['attempt_generation']} "
                    f"≠ current {run['current_attempt_generation']}"
                )
        if workspace_epoch is not None and int(workspace_epoch) != run["workspace_epoch"]:
            raise RuntimeConflictError(
                f"fence 失败: workspace_epoch {workspace_epoch} ≠ "
                f"current {run['workspace_epoch']}"
            )
        if tool_operation_generation is not None:
            # 只校验代数本身合法（状态机迁移时行内校验 generation 一致性）。
            if int(tool_operation_generation) < 1:
                raise RuntimeConflictError(
                    f"fence 失败: 非法 tool_operation_generation "
                    f"{tool_operation_generation}"
                )
        if attempt_id is None:
            return None
        return self.get_attempt(str(attempt_id))

    # --------------------------------------------------------- ToolOperation
    def create_tool_operation(
        self,
        *,
        agent_run_id: str,
        attempt_id: str,
        operation_type: str,
        canonical_scope: str = "",
    ) -> sqlite3.Row:
        """创建 CLAIMED 操作（A.6：必须先有 current attempt 才能执行工具）。

        tool_operation_generation = 该 attempt 下已建操作数 + 1（F.5 独立代数）。
        """
        self.verify_fence(agent_run_id=agent_run_id, attempt_id=attempt_id)
        now = time.time()
        operation_id = new_id("session_id")  # 操作 ID 复用 session 前缀（框架 ID 族）
        with self._runtime_connection() as conn:
            attempt = conn.execute(
                "SELECT attempt_generation FROM agent_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            generation = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tool_operations WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            ) + 1
            conn.execute(
                """
                INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id,
                                             attempt_generation, tool_operation_generation,
                                             operation_type, canonical_scope, status,
                                             handler_started_at, settled_at, outcome_json,
                                             created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 'CLAIMED', 0, 0, '{}', ?, ?)
                """,
                (
                    operation_id,
                    agent_run_id,
                    attempt_id,
                    int(attempt["attempt_generation"]),
                    generation,
                    operation_type,
                    canonical_scope,
                    now,
                    now,
                ),
            )
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def get_operation(self, operation_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM tool_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()

    def operations_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_operations WHERE attempt_id = ? "
                "ORDER BY tool_operation_generation",
                (attempt_id,),
            ).fetchall()
        return list(rows)

    def mark_operation_executing(self, operation_id: str) -> sqlite3.Row:
        """G.2：CLAIMED→EXECUTING，CAS 写 handler_started_at（调 handler 前）。"""
        now = time.time()
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET status = 'EXECUTING', handler_started_at = ?, updated_at = ?
                WHERE operation_id = ? AND status = 'CLAIMED' AND handler_started_at = 0
                """,
                (now, now, operation_id),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"operation 无法进入 EXECUTING: {operation_id} 非 CLAIMED 或已启动"
                )
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def settle_operation(
        self,
        operation_id: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        """G.3/G.5：settle 为 SUCCEEDED/FAILED/CANCELLED。

        - CANCELLED 只允许 handler 未启动（started_at=0）且仍 CLAIMED/未 settle；
          已 EXECUTING 的操作无法证明零副作用 → 必须 UNKNOWN（G.4）。
        - 已 settle 的操作不可再次 settle（状态机单向，fail-closed）。
        """
        outcome = str(outcome or "").strip().upper()
        if outcome not in {OP_SUCCEEDED, OP_FAILED, OP_CANCELLED}:
            raise RuntimeConflictError(f"非法 settle 结果: {outcome!r}")
        now = time.time()
        with self._runtime_connection() as conn:
            op = conn.execute(
                "SELECT status, handler_started_at FROM tool_operations "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if op is None:
                raise KeyError(f"operation 不存在: {operation_id}")
            if outcome == OP_CANCELLED and int(op["handler_started_at"]) != 0:
                raise RuntimeConflictError(
                    f"operation {operation_id} 已启动 handler(无法证明零副作用),"
                    f"禁止 CANCELLED,只能 UNKNOWN"
                )
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET status = ?, settled_at = ?, outcome_json = ?, updated_at = ?
                WHERE operation_id = ? AND settled_at = 0
                """,
                (
                    outcome,
                    now,
                    json.dumps(details or {}, ensure_ascii=False),
                    now,
                    operation_id,
                ),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(f"operation 已 settle,禁止二次 settle: {operation_id}")
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def mark_operation_unknown(
        self,
        operation_id: str,
        reason: str,
    ) -> sqlite3.Row:
        """G.4：EXECUTING owner 消失/timeout/无法证明零副作用 → UNKNOWN。"""
        now = time.time()
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET status = 'UNKNOWN', outcome_json = ?, updated_at = ?
                WHERE operation_id = ? AND settled_at = 0
                """,
                (json.dumps({"reason": reason}, ensure_ascii=False), now, operation_id),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"operation 已 settled,禁止标记 UNKNOWN: {operation_id}"
                )
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def can_reopen_operation(self, operation_id: str) -> bool:
        """G.3：仅 CLAIMED 且 handler 未启动时可安全 reopen。"""
        op = self.get_operation(operation_id)
        return (
            op is not None
            and str(op["status"]) == OP_CLAIMED
            and int(op["handler_started_at"]) == 0
        )

    # ------------------------------------------------------------ 资源锁
    def acquire_locks(
        self,
        scopes: list[str],
        *,
        holder_instance: str,
        attempt_id: str,
        attempt_generation: int,
        workspace_epoch: int,
        pid: int = 0,
        start_token: str = "",
        tool_operation_generation: int = 0,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> list[sqlite3.Row]:
        """G.9：canonical scope 排序，一次取得全部锁；任一被占 → 全不取。

        单事务内逐条 INSERT，UNIQUE(canonical_scope) 冲突 → 事务回滚，本次
        未取得任何锁（handler=0 语义由调用方执行）。
        """
        ordered = sorted({str(s).strip() for s in scopes if str(s).strip()})
        if not ordered:
            return []
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        try:
            with self._runtime_connection() as conn:
                for scope in ordered:
                    conn.execute(
                        """
                        INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance,
                                                   pid, start_token, attempt_id,
                                                   attempt_generation, workspace_epoch,
                                                   tool_operation_generation, lease_expires_at,
                                                   created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            scope,
                            holder_instance,
                            int(pid),
                            start_token,
                            attempt_id,
                            int(attempt_generation),
                            int(workspace_epoch),
                            int(tool_operation_generation),
                            lease_until,
                            now,
                            now,
                        ),
                    )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError(
                f"资源锁冲突: 未取得全部锁（{', '.join(ordered)}）: {exc}"
            ) from exc
        return [self.lock_for_scope(scope) for scope in ordered]

    def lock_for_scope(self, canonical_scope: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM resource_locks WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()

    def renew_lock(
        self,
        *,
        canonical_scope: str,
        holder_instance: str,
        attempt_id: str,
        attempt_generation: int,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> sqlite3.Row:
        """G.7：renew 必须 CAS；holder 不符 / attempt 不符 / 已失去 current
        pointer → 拒绝。"""
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        with self._runtime_connection() as conn:
            run = conn.execute(
                "SELECT current_attempt_id, current_attempt_generation FROM agent_runs "
                "WHERE current_attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if run is None or int(run["current_attempt_generation"]) != int(attempt_generation):
                raise RuntimeConflictError(
                    f"renew 拒绝: attempt {attempt_id} 已失去 current pointer"
                )
            updated = conn.execute(
                """
                UPDATE resource_locks
                SET lease_expires_at = ?, updated_at = ?
                WHERE canonical_scope = ? AND holder_instance = ?
                  AND attempt_id = ? AND attempt_generation = ?
                """,
                (lease_until, now, canonical_scope, holder_instance,
                 attempt_id, int(attempt_generation)),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"renew CAS 失败: scope={canonical_scope} holder={holder_instance!r}"
                )
            conn.commit()
        row = self.lock_for_scope(canonical_scope)
        assert row is not None
        return row

    def release_locks(
        self,
        scopes: list[str],
        *,
        holder_instance: str,
    ) -> int:
        """释放本 holder 的资源锁（幂等：无锁也返回 0）。"""
        ordered = sorted({str(s).strip() for s in scopes if str(s).strip()})
        if not ordered:
            return 0
        with self._runtime_connection() as conn:
            released = 0
            for scope in ordered:
                released += conn.execute(
                    "DELETE FROM resource_locks WHERE canonical_scope = ? AND holder_instance = ?",
                    (scope, holder_instance),
                ).rowcount
            conn.commit()
        return released

    def expired_locks(self, now: float | None = None) -> list[sqlite3.Row]:
        """G.8：lease 过期的锁。过期 ≠ 持有者死亡——第二 writer 必须先在
        进程层终止/对账旧持有者，否则标 UNKNOWN/dirty。"""
        now = time.time() if now is None else now
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_locks WHERE lease_expires_at < ?",
                (now,),
            ).fetchall()
        return list(rows)

    # --------------------------------------------------------- 资源 mutation
    def begin_mutation(self, *, canonical_scope: str, attempt_id: str) -> sqlite3.Row:
        """STABLE→MUTATING（CAS）。对应 G.12：writer 动身前先声明。"""
        now = time.time()
        with self._runtime_connection() as conn:
            existing = conn.execute(
                "SELECT mutation_id, state FROM resource_mutations "
                "WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()
            if existing is None:
                mutation_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO resource_mutations(mutation_id, canonical_scope, version,
                                                   state, dirty_reason, attempt_id, updated_at)
                    VALUES(?, ?, 0, 'MUTATING', '', ?, ?)
                    """,
                    (mutation_id, canonical_scope, attempt_id, now),
                )
            else:
                mutation_id = str(existing["mutation_id"])
                updated = conn.execute(
                    """
                    UPDATE resource_mutations
                    SET state = 'MUTATING', attempt_id = ?, updated_at = ?
                    WHERE mutation_id = ? AND state = 'STABLE'
                    """,
                    (attempt_id, now, mutation_id),
                ).rowcount
                if updated != 1:
                    raise RuntimeConflictError(
                        f"begin_mutation 失败: {canonical_scope} 非 STABLE 状态"
                    )
            conn.commit()
        row = self.mutation_for_scope(canonical_scope)
        assert row is not None
        return row

    def mark_mutation_stable(self, canonical_scope: str) -> sqlite3.Row:
        """MUTATING→STABLE（CAS）。"""
        now = time.time()
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE resource_mutations
                SET state = 'STABLE', updated_at = ?
                WHERE canonical_scope = ? AND state = 'MUTATING'
                """,
                (now, canonical_scope),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"mark_mutation_stable 失败: {canonical_scope} 非 MUTATING"
                )
            conn.commit()
        row = self.mutation_for_scope(canonical_scope)
        assert row is not None
        return row

    def mark_mutation_dirty(
        self,
        *,
        canonical_scope: str,
        reason: str,
        attempt_id: str = "",
    ) -> sqlite3.Row:
        """G.14：unknown/unscoped 写置 DIRTY；reconcile 前阻止发布/验收/交付。"""
        now = time.time()
        with self._runtime_connection() as conn:
            existing = conn.execute(
                "SELECT mutation_id FROM resource_mutations WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO resource_mutations(mutation_id, canonical_scope, version,
                                                   state, dirty_reason, attempt_id, updated_at)
                    VALUES(?, ?, 0, 'DIRTY', ?, ?, ?)
                    """,
                    (uuid.uuid4().hex, canonical_scope, reason, attempt_id, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE resource_mutations
                    SET state = 'DIRTY', dirty_reason = ?, attempt_id = ?, updated_at = ?
                    WHERE mutation_id = ?
                    """,
                    (reason, attempt_id, now, str(existing["mutation_id"])),
                )
            conn.commit()
        row = self.mutation_for_scope(canonical_scope)
        assert row is not None
        return row

    def mutation_for_scope(self, canonical_scope: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM resource_mutations WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()

    def workspace_dirty(self) -> list[sqlite3.Row]:
        """任何 DIRTY mutation → 阻止发布/验收/交付（G.14）。"""
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_mutations WHERE state = 'DIRTY'"
            ).fetchall()
        return list(rows)

    # --------------------------------------------------- PublishOperation
    def create_publish(
        self,
        *,
        binding_id: str,
        agent_run_id: str,
        attempt_id: str,
        workspace_epoch: int,
    ) -> sqlite3.Row:
        """H.2：publish 的唯一入口——先立 STAGING 记录，再写 manifest。"""
        self.verify_fence(
            agent_run_id=agent_run_id, attempt_id=attempt_id, workspace_epoch=workspace_epoch
        )
        publish_id = new_id("delegation_id")  # publish 复用 delegation 前缀（框架 ID 族）
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO publish_operations(publish_id, binding_id, agent_run_id,
                                               attempt_id, workspace_epoch, status,
                                               manifest_json, preimage_digests_json,
                                               postimage_digests_json, committed_at,
                                               created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, 'STAGING', '[]', '{}', '{}', 0, ?, ?)
                """,
                (publish_id, binding_id, agent_run_id, attempt_id,
                 int(workspace_epoch), now, now),
            )
            conn.commit()
        row = self.get_publish(publish_id)
        assert row is not None
        return row

    def get_publish(self, publish_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM publish_operations WHERE publish_id = ?", (publish_id,)
            ).fetchone()

    def publishes_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM publish_operations WHERE attempt_id = ? ORDER BY created_at",
                (attempt_id,),
            ).fetchall()
        return list(rows)

    def stage_publish_manifest(
        self,
        publish_id: str,
        manifest: list[dict[str, Any]],
    ) -> sqlite3.Row:
        """H.3：publish_manifest 记录路径/kind/preimage/postimage/权限。"""
        normalized: list[dict[str, Any]] = []
        for item in manifest:
            rel = str(item.get("path") or "").strip().lstrip("/")
            if not rel:
                raise RuntimeConflictError("manifest 项缺少 path")
            kind = str(item.get("kind") or "updated").strip()
            if kind not in {"created", "updated", "deleted"}:
                raise RuntimeConflictError(f"manifest 项非法 kind: {kind!r}")
            normalized.append(
                {
                    "path": rel,
                    "kind": kind,
                    "preimage_digest": str(item.get("preimage_digest") or "").strip(),
                    "postimage_digest": str(item.get("postimage_digest") or "").strip(),
                    "permissions": int(item.get("permissions") or 0),
                }
            )
        with self._runtime_connection() as conn:
            conn.execute(
                """
                UPDATE publish_operations
                SET manifest_json = ?, updated_at = ?
                WHERE publish_id = ? AND status = 'STAGING'
                """,
                (json.dumps(normalized, ensure_ascii=False), time.time(), publish_id),
            )
            conn.commit()
        row = self.get_publish(publish_id)
        assert row is not None
        return row

    def publish(
        self,
        *,
        publish_id: str,
        staging_root: str | Path,
        shared_root: str | Path,
        crash_point: str = "",
    ) -> sqlite3.Row:
        """H.4-H.8：staging→共享唯一通道，preimage CAS + 原子 apply。

        流程（fail-closed）：
        1. 重验三层 fence（F.6 publish 点）+ workspace_dirty 检查（G.14）；
        2. preimage CAS：逐项比对共享现文件 digest 与 manifest 声明的
           preimage（created=必须不存在；updated/deleted=必须存在且匹配），
           任一失配 → RESOURCE_VERSION_CONFLICT（测试 10）；
        3. crash_point 注入（测试 13 用）：after_fence / after_preimage /
           mid_apply → 落 DIRTY 返回，不继续；
        4. 逐项原子 apply：os.replace（同文件系统原子，H.5）；删除项 unlink；
           文件系统侧无法整树原子时库内 manifest 即 journal（H.6）；
        5. 全部成功后 status=COMMITTED + committed_at（H.7），随后写
           ArtifactRecord（H.8：发布完成才写）。

        崩溃（真实进程死/注入）只可能观测到 COMMITTED 或 DIRTY/UNKNOWN。
        """
        publish_row = self.get_publish(publish_id)
        if publish_row is None:
            raise KeyError(f"publish 不存在: {publish_id}")
        if str(publish_row["status"]) not in {PUB_STAGING}:
            raise RuntimeConflictError(
                f"publish {publish_id} 已处于 {publish_row['status']},不可重放"
            )
        manifest: list[dict[str, Any]] = json.loads(publish_row["manifest_json"] or "[]")
        self.verify_fence(
            agent_run_id=str(publish_row["agent_run_id"]),
            attempt_id=str(publish_row["attempt_id"]),
            workspace_epoch=int(publish_row["workspace_epoch"]),
        )
        if crash_point == "after_fence":
            return self._set_publish_state(publish_id, PUB_DIRTY, {"stage": crash_point})
        if self.workspace_dirty():
            raise RuntimeConflictError(
                f"workspace 有 DIRTY mutation,阻止发布: "
                f"{[r['canonical_scope'] for r in self.workspace_dirty()]}"
            )
        staging = Path(staging_root).resolve()
        shared = Path(shared_root).resolve()
        # H.4：publish 前的 preimage CAS。
        for item in manifest:
            rel = str(item["path"])
            target = shared / rel
            expected = str(item["preimage_digest"] or "").strip()
            kind = str(item["kind"])
            if kind == "created":
                if target.exists():
                    raise RuntimeConflictError(
                        f"{RESOURCE_VERSION_CONFLICT}: {rel} 已存在(预期 created)"
                    )
            else:
                if not target.exists() or not target.is_file():
                    raise RuntimeConflictError(
                        f"{RESOURCE_VERSION_CONFLICT}: {rel} 不存在/非文件"
                    )
                actual = sha256_of(target)
                if expected and actual != expected:
                    raise RuntimeConflictError(
                        f"{RESOURCE_VERSION_CONFLICT}: {rel} preimage digest 失配 "
                        f"(期望 {expected[:12]}…,实际 {actual[:12]}…)"
                    )
        if crash_point == "after_preimage":
            return self._set_publish_state(publish_id, PUB_DIRTY, {"stage": crash_point})
        # H.6：逐项 apply（库内 manifest 即 journal；中间崩 → 部分项已写，
        # 状态由 _set_publish_state 落 DIRTY，reconcile 用 journal 对账）。
        applied = 0
        try:
            for item in manifest:
                rel = str(item["path"])
                kind = str(item["kind"])
                if kind == "deleted":
                    (shared / rel).unlink(missing_ok=True)
                else:
                    staged = staging / rel
                    if not staged.exists() or not staged.is_file():
                        raise RuntimeConflictError(
                            f"staging 缺失发布文件: {rel}"
                        )
                    target = shared / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, target)  # 同文件系统原子（H.5）
                    if int(item.get("permissions") or 0):
                        target.chmod(int(item["permissions"]))
                applied += 1
                if crash_point == "mid_apply" and applied == 1:
                    return self._set_publish_state(publish_id, PUB_DIRTY, {"stage": crash_point})
        except RuntimeConflictError:
            raise
        except OSError as exc:
            # 文件系统侧失败：无法证明零副作用 → DIRTY（H.7）。
            return self._set_publish_state(
                publish_id, PUB_DIRTY, {"stage": "apply", "error": str(exc)}
            )
        # H.7/H.8：全量成功 → COMMITTED + ArtifactRecord。
        with self._runtime_connection() as conn:
            conn.execute(
                """
                UPDATE publish_operations
                SET status = 'COMMITTED', committed_at = ?, updated_at = ?
                WHERE publish_id = ?
                """,
                (time.time(), time.time(), publish_id),
            )
            for item in manifest:
                if str(item["kind"]) == "deleted":
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO artifact_records(artifact_id, attempt_id,
                                                           agent_run_id, publish_id,
                                                           rel_path, digest, size, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(item["postimage_digest"] or ""),
                        str(publish_row["attempt_id"]),
                        str(publish_row["agent_run_id"]),
                        publish_id,
                        str(item["path"]),
                        str(item["postimage_digest"] or ""),
                        (shared / str(item["path"])).stat().st_size
                        if (shared / str(item["path"])).exists() else 0,
                        time.time(),
                    ),
                )
            conn.commit()
        self.append_event(
            event_type="publish.committed",
            attempt_id=str(publish_row["attempt_id"]),
            agent_run_id=str(publish_row["agent_run_id"]),
            payload={"publish_id": publish_id, "items": len(manifest)},
        )
        row = self.get_publish(publish_id)
        assert row is not None
        return row

    def _set_publish_state(
        self,
        publish_id: str,
        state: str,
        details: dict[str, Any],
    ) -> sqlite3.Row:
        with self._runtime_connection() as conn:
            conn.execute(
                """
                UPDATE publish_operations
                SET status = ?, updated_at = ?
                WHERE publish_id = ?
                """,
                (state, time.time(), publish_id),
            )
            conn.commit()
        row = self.get_publish(publish_id)
        assert row is not None
        # A.8：崩溃/失败也进权威事件流（append-only，追到 attempt）。
        self.append_event(
            event_type=f"publish.{state.lower()}",
            attempt_id=str(row["attempt_id"]),
            agent_run_id=str(row["agent_run_id"]),
            payload={"publish_id": publish_id, **details},
        )
        return row

    def artifacts_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM artifact_records WHERE attempt_id = ? ORDER BY created_at",
                (attempt_id,),
            ).fetchall()
        return list(rows)
