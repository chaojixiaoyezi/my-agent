# LLM: 管理操作仍沿原运行、尝试与工具表；终态回读严格校验持久身份与结果 JSON，损坏记录不得伪装为空账。
# 模块用途: 将工具执行、资源锁和结果重放接到同一份 owner RuntimeDB。
"""B 切片：runtime.db 权威侧 OperationStore adapter（duck-typing LocalStore 契约）。

LocalStore（local_storage/tool_operations.py）是每 owner 一份的本地副作用账本；
ManagedOperationStore 把同一套 claim/finish/reopen 契约落到 owner runtime.db 的
权威 tool_operations 表 + resource_locks + resource_mutations（3.txt G/H 节）。

关键语义（与对方 B 切片契约对齐，seq 219/221/231/235/238/241）：

- claim 在同一事务内完成 attempt/generation 校验 + 幂等键 + 资源锁 +
  CLAIMED→EXECUTING 原子 start 门（无 create 与 start 之间的 takeover 窗口）。
- 缺权威字段（无 repo / run 未登记 / current attempt 为空）→
  AuthorityContextMissing（fail-closed；阶段 ② 主链 gate 映射为
  TOOL_AUTHORITY_CONTEXT_MISSING，coordinator 侧落 TOOL_OPERATION_STORE_UNAVAILABLE）。
- 同 effect_key（operation_id）已有终态行 → replay（返回持久结果，不重跑 handler）。
- 资源锁策略 fail-fast：UNIQUE(canonical_scope) 冲突 → 事务回滚 →
  RuntimeConflictError（第二操作 handler=0，不等待串行）。
- finish settle 失败（takeover/库故障）→ 行标 UNKNOWN + mutation 标 DIRTY +
  抛异常（coordinator 转不可重试 unknown，handler_executed=true 如实保留）。
- takeover 后旧 attempt 不能 begin/settle/publish（settle 的 current-pointer
  CAS 在 WHERE 里，rowcount=0 → RuntimeConflictError，此 adapter 不再吞）。

扩展字段落在 outcome_json（tool_operations 表结构不变）：holder / lease /
幂等身份 / args_hash / resource_scopes / result / error_code / unknown_reason。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.strict_json import load_strict_json
from ..local_storage.tool_operations import (
    TOOL_OPERATION_CANCELLED,
    TOOL_OPERATION_FAILED,
    TOOL_OPERATION_IDEMPOTENCY_SCOPES,
    TOOL_OPERATION_RUNNING,
    TOOL_OPERATION_SUCCEEDED,
    TOOL_OPERATION_TERMINAL_STATUSES,
    TOOL_OPERATION_UNKNOWN,
    ToolOperationClaim,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationOwnershipError,
    ToolOperationReconciliationClaimRequest,
    ToolOperationRecord,
    ToolOperationReopenRequest,
    ToolOperationStateError,
)
from .operations import (
    OP_CANCELLED,
    OP_CLAIMED,
    OP_EXECUTING,
    OP_FAILED,
    OP_SUCCEEDED,
    OP_UNKNOWN,
    RUN_STATUS_LEGACY_CREATED,
    RuntimeConflictError,
)

_OUTCOME_SCHEMA = "managed_operation.v1"

_STATUS_TO_RUNTIME = {
    OP_CLAIMED: TOOL_OPERATION_RUNNING,
    OP_EXECUTING: TOOL_OPERATION_RUNNING,
    OP_SUCCEEDED: TOOL_OPERATION_SUCCEEDED,
    OP_FAILED: TOOL_OPERATION_FAILED,
    "CANCELLED": TOOL_OPERATION_CANCELLED,
    OP_UNKNOWN: TOOL_OPERATION_UNKNOWN,
}


class AuthorityContextMissing(ToolOperationStateError):
    """MANAGED 权威链缺失（无 repo / run 未登记 / current attempt 为空）。

    fail-closed 信号：executor 权威门按其映射 TOOL_AUTHORITY_CONTEXT_MISSING
    + handler=0；coordinator claim 兜底同映射。
    """


@dataclass(frozen=True)
class ToolOperationAuthorityRequest:
    """权威门轻请求（只查权威链，不建操作行）。

    ``attempt_id`` 是宿主注入的可信调用者身份（seq 248 #1）：MANAGED 下
    attempt 为空必须拒绝，且 attempt 必须仍是该 run 的 current attempt——
    绝不能用「数据库当前是谁」替调用者补身份（takeover 后旧 attempt 的
    调用一律拦截，含 read-only）。
    """

    owner_id: str
    run_id: str
    task_id: str
    operation_id: str
    tool_name: str
    attempt_id: str = ""


# LLM: 所有副作用依赖原 attempt 与资源 fence；回读只核对事实，不授予终态尝试新的执行权。
# 类用途: 为原工具执行器提供持久领取、提交、核对及精确结果读取。
class ManagedOperationStore:
    """runtime.db 权威侧的 OperationStore（claim/finish/reopen 三件套）。"""

    def __init__(self, repo: object | None) -> None:
        self._repo = repo

    # ------------------------------------------------------------- authority 门
    # LLM: 默认分支是原 preclaim 运行权检查；显式 claim/scopes 只读核对同一持有者和锁，不为调用方补新代次。
    # 函数用途: 在工具进入前检查运行权，或在长准备过程中重复核对已领取的精确资源。
    def require_authority(self, request: ToolOperationAuthorityRequest, *,
                          claim: ToolOperationRecord | None = None, resource_scopes: tuple[str, ...] = ()) -> None:
        """MANAGED 权威门：缺 repo / run 未登记 / attempt 非 current → 抛错。

        所有工具（含 read-only）在 handler 前过此门；read-only 只跳过副作用
        operation，不绕过 authority（l 契约）。不懒建链（a2：run 未登记 →
        拦截且零残留，链只由 create_run 登记）。

        一次 join 校验（seq 248 #1/#4）：调用者 attempt 经 agent_attempts
        锚定 agent_run（root/child 同 run_id 时不再歧义）→ 同一事务读取
        current attempt 指针 → 调用者声明的 task_id（非空时）必须与权威链
        tasks.task_id 一致。
        """
        if claim is not None:
            from .operation_resources import require_claimed_resources

            require_claimed_resources(self._repo, request, claim, resource_scopes)
            return
        if resource_scopes:
            raise AuthorityContextMissing("资源检查缺少原操作领取记录")
        if self._repo is None:
            raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
        attempt_id = str(request.attempt_id or "")
        if not attempt_id:
            raise AuthorityContextMissing(
                f"MANAGED 工具调用缺少调用者 attempt: {request.run_id}"
            )
        with self._repo._runtime_connection() as conn:
            row = conn.execute(
                """
                SELECT ar.current_attempt_id, ar.current_attempt_generation,
                       ar.status AS agent_run_status,
                       at.status AS attempt_status, tr.task_id
                FROM agent_runs ar
                JOIN agent_attempts at
                  ON at.attempt_id = ? AND at.agent_run_id = ar.agent_run_id
                JOIN task_runs tr ON tr.task_run_id = ar.task_run_id
                WHERE ar.run_id = ?
                """,
                (attempt_id, request.run_id),
            ).fetchone()
        if row is None:
            raise AuthorityContextMissing(f"run 未登记权威链: {request.run_id}")
        task_id = str(request.task_id or "").strip()
        if task_id and task_id != str(row["task_id"] or ""):
            raise AuthorityContextMissing(
                f"调用者 task {task_id} 与权威链不符 {row['task_id']}"
            )
        current = str(row["current_attempt_id"] or "")
        if not current:
            raise AuthorityContextMissing(f"run 无 current attempt: {request.run_id}")
        if current != attempt_id:
            raise AuthorityContextMissing(
                f"调用者 attempt {attempt_id} 不是 current attempt {current}"
            )
        _require_running_authority(row, request.run_id, attempt_id)

    # ------------------------------------------------------------- claim
    def claim_tool_operation(
        self,
        request: ToolOperationClaimRequest,
    ) -> ToolOperationClaim:
        _validate_claim_request(request)
        if self._repo is None:
            raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
        now = float(request.now or time.time())
        attempt_id = str(request.attempt_id or "").strip()
        if not attempt_id:
            raise AuthorityContextMissing(
                f"MANAGED 工具调用缺少调用者 attempt: {request.run_id}"
            )
        with self._repo._runtime_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # attempt 锚定（seq 248 #1/#4）：调用者 attempt 经 agent_attempts
            # 定位 agent_run（root/child 同 run_id 不再歧义），同一事务读
            # current pointer + 权威 task_id；空 task 声明跳过比对（attempt
            # 锚定已保证权威，run_scope 兜底 run_id 不是 task 声明）。
            run = conn.execute(
                """
                SELECT ar.agent_run_id, ar.current_attempt_id,
                       ar.current_attempt_generation, ar.workspace_epoch,
                       ar.status AS agent_run_status,
                       at.status AS attempt_status, tr.task_id
                FROM agent_runs ar
                JOIN agent_attempts at
                  ON at.attempt_id = ? AND at.agent_run_id = ar.agent_run_id
                JOIN task_runs tr ON tr.task_run_id = ar.task_run_id
                WHERE ar.run_id = ?
                """,
                (attempt_id, request.run_id),
            ).fetchone()
            if run is None:
                raise AuthorityContextMissing(f"run 未登记权威链: {request.run_id}")
            task_id = str(request.task_id or "").strip()
            if task_id and task_id != str(run["task_id"] or ""):
                raise AuthorityContextMissing(
                    f"调用者 task {task_id} 与权威链不符 {run['task_id']}"
                )
            current = str(run["current_attempt_id"] or "")
            if not current:
                raise AuthorityContextMissing(f"run 无 current attempt: {request.run_id}")
            if current != attempt_id:
                raise AuthorityContextMissing(
                    f"调用者 attempt {attempt_id} 不是 current attempt {current}"
                )
            _require_running_authority(run, request.run_id, attempt_id)
            existing = conn.execute(
                "SELECT * FROM tool_operations WHERE operation_id = ?",
                (request.operation_id,),
            ).fetchone()
            if existing is not None:
                decision = _existing_operation_decision(conn, existing, request, now)
                conn.commit()
                return decision
            _insert_executing_operation(conn, run, request, now)
            conn.commit()
        record = _operation_record(
            self._repo,
            request.operation_id,
            owner_id=request.owner_id,
            run_id=request.run_id,
            task_id=request.task_id,
        )
        return ToolOperationClaim("execute", record)

    # ------------------------------------------------------------- finish
    def finish_tool_operation(
        self,
        request: ToolOperationCompletionRequest,
    ) -> ToolOperationRecord:
        if request.status not in (
            TOOL_OPERATION_TERMINAL_STATUSES | {TOOL_OPERATION_UNKNOWN}
        ):
            raise ValueError(
                f"invalid settled tool operation status: {request.status}"
            )
        if self._repo is None:
            raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
        # seq 253 #1：completion 空 holder 必须无条件拒绝——空值 ≠ 跳过校验，
        # 恒等比较是 settle 的唯一闸门，不设「未提供即放行」旁路。
        if not str(request.holder_id or "").strip():
            raise RuntimeConflictError(
                f"settle 拒绝: operation {request.operation_id} 缺少 holder 标识"
            )
        try:
            if request.status == TOOL_OPERATION_UNKNOWN:
                reason = str(request.unknown_reason or "") or "effect_outcome_unknown"
                _mark_unknown_single_transaction(
                    self._repo,
                    request.operation_id,
                    holder_id=str(request.holder_id or ""),
                    reason=reason,
                    expected_generation=int(request.generation or 0),
                )
            else:
                outcome = (
                    OP_SUCCEEDED
                    if request.status == TOOL_OPERATION_SUCCEEDED
                    else OP_FAILED
                )
                # seq 248 #2：details 注入 holder/generation CAS 期望（settle
                # 内部 pop，不进 outcome_json）。
                details = _completion_payload(self._repo, request)
                details["_holder_id"] = str(request.holder_id or "")
                details["_expected_generation"] = int(request.generation or 0)
                _settle_operation_via_repo(
                    self._repo,
                    request.operation_id,
                    outcome=outcome,
                    details=details,
                )
        except RuntimeConflictError:
            # seq 253 #2：身份/代数/epoch 冲突被拒 ≠ 执行结果不明——冲突不改写
            # 当前权威行（保持原状态让真 holder 继续/收口）；只有真实持久化
            # 故障（下一个 except）才可 UNKNOWN。
            raise
        except Exception as exc:
            _mark_settle_failure(
                self._repo,
                request.operation_id,
                holder_id=str(request.holder_id or ""),
                expected_generation=int(request.generation or 0),
                reason=f"settle_crash:{type(exc).__name__}",
            )
            raise
        record = _operation_record(
            self._repo,
            request.operation_id,
            owner_id=request.owner_id,
            run_id=request.run_id,
            task_id="",
        )
        return record

    # LLM: UNKNOWN reconciliation can mutate provider receipts or owner-private journals, so it
    # needs the same cross-process authority as execution. The row stays UNKNOWN until facts prove
    # a terminal result; holder/lease and workspace locks fence all competing reconcilers.
    # 函数用途: 在 runtime.db 内原子领取未知副作用的核对权，并重新取得原资源范围的锁。
    def claim_tool_operation_reconciliation(
        self,
        request: ToolOperationReconciliationClaimRequest,
    ) -> ToolOperationClaim:
        return _claim_managed_tool_operation_reconciliation(self._repo, request)

    # LLM: A still-unknown reconciliation must relinquish its exact lease and workspace locks.
    # The mutation remains DIRTY, preserving the fail-closed fact for a later reconciler.
    # 函数用途: 未核对出终态时原子释放核对权，让后续调用可继续恢复而不重复并发执行。
    def release_tool_operation_reconciliation(
        self,
        request: ToolOperationReconciliationClaimRequest,
    ) -> ToolOperationRecord:
        if self._repo is None:
            raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
        current = float(request.now if request.now is not None else time.time())
        with self._repo._runtime_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tool_operations WHERE operation_id = ?",
                (request.operation_id,),
            ).fetchone()
            if row is None:
                raise ToolOperationOwnershipError(
                    "tool operation reconciliation release target is missing"
                )
            payload = _outcome_payload(row)
            holder = payload.get("holder") if isinstance(payload.get("holder"), dict) else {}
            if (
                str(row["status"] or "") != OP_UNKNOWN
                or int(row["tool_operation_generation"]) != int(request.expected_generation)
                or str(holder.get("holder_id") or "") != request.holder.holder_id
            ):
                raise ToolOperationOwnershipError(
                    "tool operation reconciliation release lost ownership"
                )
            payload.pop("reconciliation_claim", None)
            payload["lease_expires_at"] = 0.0
            scopes = _scopes_from_payload(payload)
            conn.execute(
                "UPDATE tool_operations SET outcome_json = ?, updated_at = ? "
                "WHERE operation_id = ? AND status = ? AND "
                "tool_operation_generation = ?",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    current,
                    request.operation_id,
                    OP_UNKNOWN,
                    int(request.expected_generation),
                ),
            )
            _delete_locks_in_tx(conn, scopes, holder_id=request.holder.holder_id)
            for scope in scopes:
                _mark_mutation_dirty_in_tx(
                    conn,
                    scope,
                    reason="reconciliation_incomplete",
                    attempt_id=str(row["attempt_id"] or ""),
                    now=current,
                )
            conn.commit()
        return _operation_record(
            self._repo,
            request.operation_id,
            owner_id=request.owner_id,
            run_id=request.run_id,
            task_id="",
        )

    # ------------------------------------------------------------- reopen
    def reopen_tool_operation_after_reconciliation(
        self,
        request: ToolOperationReopenRequest,
    ) -> ToolOperationRecord:
        """对账后的重开：复用 claim/start 的完整事务语义（seq 245 P6）。

        单事务内：current-attempt CAS + 代数 CAS → 行回 EXECUTING（generation+1）
        → 重取资源锁（fail-fast）→ mutation 重声明 MUTATING。takeover 后旧
        attempt 的 reconcile 重开被 current-attempt CAS 拒绝
        （ToolOperationOwnershipError，与 claim/settle 的 fence 一致）。
        """
        if not str(request.source_ref or "").strip():
            raise ValueError("tool operation reconciliation source_ref is required")
        current = float(request.now if request.now is not None else time.time())
        if float(request.lease_expires_at or 0) <= current:
            raise ValueError("reopened tool operation lease must expire in the future")
        if self._repo is None:
            raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
        with self._repo._runtime_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT op.*, ar.workspace_epoch AS _workspace_epoch
                FROM tool_operations op
                JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id
                WHERE op.operation_id = ? AND op.status = ?
                  AND op.tool_operation_generation = ?
                  AND op.attempt_id = ar.current_attempt_id
                  AND op.attempt_generation = ar.current_attempt_generation
                """,
                (request.operation_id, OP_UNKNOWN, int(request.expected_generation)),
            ).fetchone()
            if row is None:
                raise ToolOperationOwnershipError(
                    f"tool operation reconciliation 被拒: {request.operation_id} "
                    f"已非 current attempt 或未知代数（takeover 后旧 attempt 的 "
                    f"reconcile 重开必须拒绝）"
                )
            payload = _outcome_payload(row)
            holder = payload.get("holder") if isinstance(payload.get("holder"), dict) else {}
            marker = payload.get("reconciliation_claim")
            if (
                isinstance(marker, dict)
                and str(holder.get("holder_id") or "") != request.holder.holder_id
            ):
                raise ToolOperationOwnershipError(
                    "tool operation reconciliation reopen lost holder ownership"
                )
            # seq 253 #3：reopen 必须对当前 workspace_epoch 做 fence——claim
            # 捕获 epoch 与当前 epoch 不一致（工作区已翻新）→ 拒绝，否则形成
            # 「可执行、不可收口」状态（后续 settle 必因 epoch 不符失败）。
            captured_epoch = int(payload.get("workspace_epoch") or 0)
            if captured_epoch != int(row["_workspace_epoch"]):
                raise RuntimeConflictError(
                    f"reopen 拒绝: operation {request.operation_id} "
                    f"workspace_epoch 已翻新"
                    f"(claim epoch={captured_epoch}, 当前={row['_workspace_epoch']})"
                )
            payload.update(
                {
                    "schema": _OUTCOME_SCHEMA,
                    "holder": _holder_dict(request.holder),
                    "lease_expires_at": float(request.lease_expires_at),
                    "reconciliation": {
                        "source_ref": str(request.source_ref or ""),
                        "result": _json_payload(request.reconciliation_result),
                    },
                    "result": {},
                    "error_code": "",
                    "unknown_reason": "",
                }
            )
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET status = ?, handler_started_at = ?, tool_operation_generation = ?,
                    outcome_json = ?, updated_at = ?
                WHERE operation_id = ? AND status = ? AND tool_operation_generation = ?
                """,
                (
                    OP_EXECUTING,
                    current,
                    int(row["tool_operation_generation"]) + 1,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    current,
                    request.operation_id,
                    OP_UNKNOWN,
                    int(request.expected_generation),
                ),
            ).rowcount
            if updated != 1:
                raise ToolOperationOwnershipError(
                    f"tool operation reconciliation lost unknown generation: "
                    f"{request.operation_id}"
                )
            # 重取资源锁（fail-fast）+ mutation 重声明 MUTATING，与 claim 同事务形状。
            scopes = _scopes_from_payload(payload)
            new_generation = int(row["tool_operation_generation"]) + 1
            _delete_locks_in_tx(
                conn,
                scopes,
                holder_id=request.holder.holder_id,
            )
            try:
                for scope in scopes:
                    _insert_lock_in_tx(
                        conn,
                        scope,
                        request.holder,
                        attempt_id=str(row["attempt_id"]),
                        attempt_generation=int(row["attempt_generation"]),
                        workspace_epoch=int(row["_workspace_epoch"]),
                        tool_operation_generation=new_generation,
                        lease_expires_at=float(request.lease_expires_at),
                        now=current,
                    )
            except sqlite3.IntegrityError as exc:
                raise RuntimeConflictError(
                    f"资源锁冲突: 未取得全部锁（{', '.join(scopes)}）: {exc}"
                ) from exc
            for scope in scopes:
                _upsert_mutation_mutating_in_tx(
                    conn, scope, attempt_id=str(row["attempt_id"]), now=current
                )
            conn.commit()
        record = _operation_record(
            self._repo,
            request.operation_id,
            owner_id=request.owner_id,
            run_id=request.run_id,
            task_id="",
        )
        return record

    # ------------------------------------------------------------- renew
    def renew_tool_operation_lease(
        self,
        *,
        operation_id: str,
        holder_id: str,
        lease_expires_at: float,
        owner_id: str = "",
        run_id: str = "",
    ) -> ToolOperationRecord:
        """续租：操作行 + 其全部资源锁的 lease 同一事务延长（holder CAS，seq 245 P6）。

        只有 outcome_json 中记录的操作持有者本人可续（holder_id 相等）；操作
        不存在 / holder 不符 → ToolOperationOwnershipError；已终态 → 拒绝
        （终态后锁已随事务删除，续租无意义）。单事务避免行 lease 与锁 lease
        分叉（行已续而锁过期 → 被第二 writer 当作可回收，G.8 冲突）。
        """
        if float(lease_expires_at or 0) <= time.time():
            raise ValueError("renewed tool operation lease must expire in the future")
        if self._repo is None:
            raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
        now = time.time()
        with self._repo._runtime_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT op.*, ar.workspace_epoch AS _current_epoch
                FROM tool_operations op
                JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id
                WHERE op.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ToolOperationOwnershipError(
                    f"tool operation 不存在: {operation_id}"
                )
            payload = _outcome_payload(row)
            holder = payload.get("holder") if isinstance(payload.get("holder"), dict) else {}
            if str(holder.get("holder_id") or "") != str(holder_id or ""):
                raise ToolOperationOwnershipError(
                    f"tool operation 续租被拒: holder 不符 {operation_id}"
                )
            # seq 253 #4：renew 必须 join agent_runs 当前 workspace_epoch 做 CAS
            # ——工作区已翻新时旧操作的续租（其锁绑旧 epoch）毫无意义，且会掩盖
            # 已失效 holder 的存活假象。
            if int(payload.get("workspace_epoch") or 0) != int(row["_current_epoch"]):
                raise RuntimeConflictError(
                    f"tool operation 续租被拒: workspace_epoch 已翻新 {operation_id}"
                )
            payload["lease_expires_at"] = float(lease_expires_at)
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET outcome_json = ?, updated_at = ?
                WHERE operation_id = ?
                  AND settled_at = 0 AND status = ?
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    operation_id,
                    OP_EXECUTING,
                ),
            ).rowcount
            if updated != 1:
                raise ToolOperationStateError(
                    f"tool operation 已终态,禁止续租: {operation_id}"
                )
            # seq 248 #7：锁 UPDATE 同代数 CAS（attempt/generation/epoch/tool
            # generation 与行一致）+ rowcount 校验——续租不再只看 holder_instance，
            # 行与锁的代数对不上（takeover 重建/过期回收后）一律拒绝。
            scopes = _scopes_from_payload(payload)
            for scope in scopes:
                lock_updated = conn.execute(
                    """
                    UPDATE resource_locks
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE canonical_scope = ?
                      AND holder_instance = ?
                      AND attempt_id = ?
                      AND attempt_generation = ?
                      AND workspace_epoch = ?
                      AND tool_operation_generation = ?
                    """,
                    (
                        float(lease_expires_at),
                        now,
                        scope,
                        str(holder_id or ""),
                        str(row["attempt_id"] or ""),
                        int(row["attempt_generation"] or 0),
                        int(payload.get("workspace_epoch") or 0),
                        int(row["tool_operation_generation"] or 0),
                    ),
                ).rowcount
                if lock_updated != 1:
                    raise ToolOperationStateError(
                        f"tool operation 续租失败: 锁 {scope} 代数不符或已释放 "
                        f"({operation_id})"
                    )
            conn.commit()
        return _operation_record(
            self._repo,
            operation_id,
            owner_id=str(owner_id or ""),
            run_id=str(run_id or ""),
            task_id="",
        )

    # --------------------------------------------- read 面（对账/观测用）
    # LLM: 回读联查原 Task→TaskRun→AgentRun→Attempt；可核验原持久资源声明，坏 JSON/未知协议/身份失配均拒绝。
    # 函数用途: 严格读取原操作、输入及指定资源归属；历史终态不重开执行权，损坏记录不冒充未执行。
    def get_tool_operation(
        self,
        *,
        owner_id: str,
        run_id: str,
        operation_id: str,
        task_id: str = "",
        attempt_id: str = "",
        tool_name: str = "",
        args_hash: str = "",
        resource_scopes: tuple[str, ...] | None = None,
    ) -> ToolOperationRecord | None:
        if self._repo is None:
            return None
        with self._repo._runtime_connection() as conn:
            row = conn.execute(
                "SELECT op.*, ar.run_id AS _run_id, t.owner_id AS _owner_id, "
                "t.task_id AS _task_id, at.attempt_id AS _attempt_id "
                "FROM tool_operations op "
                "LEFT JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id "
                "LEFT JOIN task_runs tr ON tr.task_run_id = ar.task_run_id "
                "LEFT JOIN tasks t ON t.task_id = tr.task_id "
                "LEFT JOIN agent_attempts at ON at.attempt_id = op.attempt_id "
                "AND at.agent_run_id = op.agent_run_id AND at.attempt_generation = op.attempt_generation "
                "WHERE op.operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        payload = _checked_operation_payload(row["outcome_json"])
        if (
            not owner_id or not run_id or not row["_task_id"] or not row["_attempt_id"]
            or row["_owner_id"] != owner_id or row["_run_id"] != run_id
            or (task_id and row["_task_id"] != task_id)
            or (attempt_id and row["_attempt_id"] != attempt_id)
            or (tool_name and row["operation_type"] != tool_name)
            or (args_hash and payload.get("args_hash") != args_hash)
            or (resource_scopes is not None and not _matching_resource_scopes(payload, resource_scopes))
        ):
            raise ToolOperationStateError("原工具操作的归属或输入与查询不符")
        return _record_from_row(
            row, owner_id=row["_owner_id"], run_id=row["_run_id"], task_id=row["_task_id"],
        )

    def list_tool_operations(
        self,
        *,
        owner_id: str = "",
        run_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[ToolOperationRecord]:
        if limit <= 0 or self._repo is None:
            return []
        clauses: list[str] = []
        values: list[object] = []
        if str(run_id or "").strip():
            clauses.append("ar.run_id = ?")
            values.append(str(run_id))
        if str(status or "").strip():
            clauses.append("op.status = ?")
            values.append(str(status).strip().upper())
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._repo._runtime_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT op.operation_id FROM tool_operations op
                JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id
                {where}
                ORDER BY op.created_at ASC, op.operation_id ASC
                LIMIT ?
                """,
                [*values, int(limit)],
            ).fetchall()
        records: list[ToolOperationRecord] = []
        for row in rows:
            record = _operation_record(
                self._repo,
                str(row["operation_id"]),
                owner_id=str(owner_id or ""),
                run_id=str(run_id or ""),
                task_id="",
            )
            if record is not None:
                records.append(record)
        return records


# LLM: Managed UNKNOWN reconciliation is one runtime.db transaction: current-attempt fence,
# exclusive lease, workspace locks, mutation ownership, and payload claim advance together.
# 函数用途: 在 runtime.db 中原子领取未知副作用核对权，并返回新的权威 holder 记录。
def _claim_managed_tool_operation_reconciliation(
    repo: Any,
    request: ToolOperationReconciliationClaimRequest,
) -> ToolOperationClaim:
    current = float(request.now if request.now is not None else time.time())
    if float(request.lease_expires_at or 0) <= current:
        raise ValueError("tool operation reconciliation lease must expire in the future")
    if repo is None:
        raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
    with repo._runtime_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT op.*, ar.workspace_epoch AS _workspace_epoch
            FROM tool_operations op
            JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id
            WHERE op.operation_id = ?
              AND op.attempt_id = ar.current_attempt_id
              AND op.attempt_generation = ar.current_attempt_generation
            """,
            (request.operation_id,),
        ).fetchone()
        if row is None:
            raise ToolOperationOwnershipError(
                f"tool operation reconciliation target is missing or fenced: "
                f"{request.operation_id}"
            )
        blocked = _managed_reconciliation_claim_decision(row, request, current)
        if blocked is not None:
            conn.commit()
            return blocked
        payload = _outcome_payload(row)
        scopes = _scopes_from_payload(payload)
        if not _acquire_managed_reconciliation_scopes(
            conn,
            row,
            request,
            scopes,
            current,
        ):
            conn.rollback()
            return ToolOperationClaim(
                "in_flight",
                _record_from_row(row, owner_id=request.owner_id),
                "reconciliation_scope_busy",
            )
        payload["holder"] = _holder_dict(request.holder)
        payload["lease_expires_at"] = float(request.lease_expires_at)
        payload["reconciliation_claim"] = {
            "holder": _holder_dict(request.holder),
            "lease_expires_at": float(request.lease_expires_at),
            "generation": int(row["tool_operation_generation"]),
        }
        updated = conn.execute(
            "UPDATE tool_operations SET outcome_json = ?, updated_at = ? "
            "WHERE operation_id = ? AND status = ? AND "
            "tool_operation_generation = ? AND settled_at = 0",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                current,
                request.operation_id,
                OP_UNKNOWN,
                int(request.expected_generation),
            ),
        ).rowcount
        if updated != 1:
            raise ToolOperationOwnershipError(
                "tool operation reconciliation claim lost unknown generation"
            )
        for scope in scopes:
            _upsert_mutation_mutating_in_tx(
                conn, scope, attempt_id=str(row["attempt_id"]), now=current
            )
        conn.commit()
    claimed = _operation_record(
        repo,
        request.operation_id,
        owner_id=request.owner_id,
        run_id=request.run_id,
        task_id="",
    )
    return ToolOperationClaim("reconcile", claimed)


# LLM: This decision reads only the row locked by BEGIN IMMEDIATE; no prose or caller hint can
# bypass status, generation, or a live reconciliation lease.
# 函数用途: 判断一条 managed UNKNOWN 行是否暂时不能被当前核对者领取。
def _managed_reconciliation_claim_decision(
    row: sqlite3.Row,
    request: ToolOperationReconciliationClaimRequest,
    current: float,
) -> ToolOperationClaim | None:
    record = _record_from_row(row, owner_id=request.owner_id)
    if str(row["status"] or "") != OP_UNKNOWN:
        return ToolOperationClaim("conflict", record, "operation_is_not_unknown")
    if int(row["tool_operation_generation"]) != int(request.expected_generation):
        return ToolOperationClaim("conflict", record, "unknown_generation_changed")
    marker = _outcome_payload(row).get("reconciliation_claim")
    if isinstance(marker, dict) and _managed_reconciliation_claim_is_live(marker, current):
        return ToolOperationClaim("in_flight", record, "reconciliation_in_flight")
    return None


# LLM: Resource locks are reacquired before the reconciliation marker is committed. Any overlap
# rolls back the transaction and leaves the UNKNOWN row untouched.
# 函数用途: 为 managed 未知操作核对重新取得其原始结构化资源锁。
def _acquire_managed_reconciliation_scopes(
    conn: Any,
    row: sqlite3.Row,
    request: ToolOperationReconciliationClaimRequest,
    scopes: list[str],
    current: float,
) -> bool:
    try:
        for scope in scopes:
            _insert_lock_in_tx(
                conn,
                scope,
                request.holder,
                attempt_id=str(row["attempt_id"]),
                attempt_generation=int(row["attempt_generation"]),
                workspace_epoch=int(row["_workspace_epoch"]),
                tool_operation_generation=int(row["tool_operation_generation"]),
                lease_expires_at=float(request.lease_expires_at),
                now=current,
            )
    except (RuntimeConflictError, sqlite3.IntegrityError):
        return False
    return True


# ------------------------------------------------------------------ claim 内部

# LLM: current pointer alone is insufficient authority because a terminal run
# deliberately keeps that pointer for audit. Every handler-entry path must also
# prove the exact attempt is running and the AgentRun remains active.
# 函数用途: 在只读权威门和写工具 claim 的同一数据库快照中阻止终态继续执行。
def _require_running_authority(row: sqlite3.Row, run_id: str, attempt_id: str) -> None:
    run_status = str(row["agent_run_status"] or "")
    attempt_status = str(row["attempt_status"] or "")
    if run_status not in RUN_STATUS_LEGACY_CREATED or attempt_status != "running":
        raise AuthorityContextMissing(
            "MANAGED 工具调用已失去运行权: "
            f"run={run_id} status={run_status or 'created'} "
            f"attempt={attempt_id} attempt_status={attempt_status or 'unknown'}"
        )


def _validate_claim_request(request: ToolOperationClaimRequest) -> None:
    required = {
        "owner_id": request.owner_id,
        "run_id": request.run_id,
        "operation_id": request.operation_id,
        "tool": request.tool,
        "args_hash": request.args_hash,
        "idempotency_key": request.idempotency_key,
        "idempotency_namespace": request.idempotency_namespace,
        "holder_id": request.holder.holder_id,
    }
    missing = sorted(
        name for name, value in required.items() if not str(value or "").strip()
    )
    if missing:
        raise ValueError(
            f"tool operation claim missing fields: {','.join(missing)}"
        )
    if request.idempotency_scope not in TOOL_OPERATION_IDEMPOTENCY_SCOPES:
        raise ValueError(
            f"invalid tool operation idempotency scope: {request.idempotency_scope}"
        )
    if float(request.lease_expires_at or 0) <= float(request.now or time.time()):
        raise ValueError("tool operation lease must expire in the future")


def _insert_executing_operation(
    conn: Any,
    run: sqlite3.Row,
    request: ToolOperationClaimRequest,
    now: float,
) -> None:
    """同一事务：INSERT CLAIMED → 原子 start 门置 EXECUTING → 资源锁 → mutation 声明。

    - attempt/generation 以当前 run 行为准（行是本事务刚读的 current pointer，
      create 与 start 之间无 takeover 间隙）。
    - 资源锁 UNIQUE(canonical_scope) 冲突 → IntegrityError → 事务回滚 →
      RuntimeConflictError（fail-fast，调用方 handler=0）。
    """
    generation = int(
        conn.execute(
            "SELECT COUNT(*) FROM tool_operations WHERE attempt_id = ?",
            (str(run["current_attempt_id"]),),
        ).fetchone()[0]
    ) + 1
    operation_id = request.operation_id
    payload = _claim_payload(request, workspace_epoch=int(run["workspace_epoch"]))
    conn.execute(
        """
        INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id,
                                     attempt_generation, tool_operation_generation,
                                     operation_type, canonical_scope, status,
                                     handler_started_at, settled_at, outcome_json,
                                     created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, '', 'CLAIMED', 0, 0, ?, ?, ?)
        """,
        (
            operation_id,
            str(run["agent_run_id"]),
            str(run["current_attempt_id"]),
            int(run["current_attempt_generation"]),
            generation,
            str(request.tool or "tool"),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    updated = conn.execute(
        """
        UPDATE tool_operations
        SET status = 'EXECUTING', handler_started_at = ?, updated_at = ?
        WHERE operation_id = ? AND status = 'CLAIMED' AND handler_started_at = 0
        """,
        (now, now, operation_id),
    ).rowcount
    if updated != 1:
        raise RuntimeConflictError(f"operation 无法进入 EXECUTING: {operation_id}")
    _acquire_scopes(conn, run, request, now, tool_operation_generation=generation)


def _acquire_scopes(
    conn: Any,
    run: sqlite3.Row,
    request: ToolOperationClaimRequest,
    now: float,
    *,
    tool_operation_generation: int,
) -> None:
    """资源锁（fail-fast）+ mutation 声明（MUTATING），同一事务。"""
    scopes = sorted(
        {str(scope).strip() for scope in request.resource_scopes if str(scope).strip()}
    )
    try:
        for scope in scopes:
            _insert_lock_in_tx(
                conn,
                scope,
                request.holder,
                attempt_id=str(run["current_attempt_id"]),
                attempt_generation=int(run["current_attempt_generation"]),
                workspace_epoch=int(run["workspace_epoch"]),
                tool_operation_generation=tool_operation_generation,
                lease_expires_at=float(request.lease_expires_at),
                now=now,
            )
    except sqlite3.IntegrityError as exc:
        raise RuntimeConflictError(
            f"资源锁冲突: 未取得全部锁（{', '.join(scopes)}）: {exc}"
        ) from exc
    for scope in scopes:
        _upsert_mutation_mutating_in_tx(
            conn, scope, attempt_id=str(run["current_attempt_id"]), now=now
        )


# -------------------------------------------------------------- 事务内联原语
# seq 245 P3/P4：store 的所有副作用写必须与操作行同一连接同一事务（外层
# BEGIN IMMEDIATE 未提交时开第二连接写会撞写锁 SQLITE_BUSY；三段分开提交在
# 间隙崩溃会留下「终态行 + 锁仍在/状态悬空」）。以下 helper 只在调用方已
# BEGIN IMMEDIATE 的 conn 上工作，绝不自己开连接。

def _insert_lock_in_tx(
    conn: Any,
    scope: str,
    holder: object,
    *,
    attempt_id: str,
    attempt_generation: int,
    workspace_epoch: int,
    tool_operation_generation: int,
    lease_expires_at: float,
    now: float,
) -> None:
    """单事务内插入一条资源锁（fail-fast 由调用方包 IntegrityError）。

    seq 248 #6：UNIQUE(canonical_scope) 只拦精确串——workspace 锁的物理
    重叠（parent/child 层级、hardlink 同 inode）在此显式判定：锁语义是
    「同一物理写根互斥」，串不同不代表根不同。
    """
    _check_workspace_overlap_in_tx(conn, scope, attempt_id=attempt_id)
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
            str(getattr(holder, "holder_id", "") or ""),
            int(getattr(holder, "pid", 0) or 0),
            str(getattr(holder, "process_start_token", "") or ""),
            attempt_id,
            int(attempt_generation),
            int(workspace_epoch),
            int(tool_operation_generation),
            float(lease_expires_at),
            now,
            now,
        ),
    )


def _check_workspace_overlap_in_tx(conn: Any, scope: str, *, attempt_id: str = "") -> None:
    """workspace 锁层级重叠 + hardlink 同 inode 冲突检测（fail-fast）。

    只对 workspace: 物理根锁判定；logical 锁是文本互斥，无物理层级。
    存在性不可判的（路径不存在）仅 parent/child 覆盖；inode 判定只在
    两端都是现存普通文件时成立（hardlink 只对文件有意义）。
    WRITE-04(2026-08-15 真机): 同一 attempt 内声明的父子 scope(cwd=task_root +
    写根=work)是同一执行者的合法资源声明, 不是并发冲突——重叠检测跳过同
    attempt 已插入的锁; 跨 attempt/执行者的重叠仍拦截(并发保护不变)。
    """
    if not scope.startswith("workspace:"):
        return
    candidate = _norm_lock_path(scope[len("workspace:"):])
    rows = conn.execute(
        "SELECT canonical_scope FROM resource_locks WHERE attempt_id != ?",
        (str(attempt_id or ""),),
    ).fetchall()
    for row in rows:
        existing = str(row["canonical_scope"] or "")
        if not existing.startswith("workspace:"):
            continue
        existing_path = _norm_lock_path(existing[len("workspace:"):])
        if _paths_overlap(candidate, existing_path):
            raise RuntimeConflictError(
                f"资源锁层级冲突: {candidate} 与已锁 {existing_path} 物理重叠"
            )


def _norm_lock_path(raw: str) -> Path:
    try:
        return Path(raw).resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(raw)


def _paths_overlap(a: Path, b: Path) -> bool:
    """物理根重叠：精确同根 / parent-child 层级 / hardlink 同 inode。"""
    if a == b:
        return True
    if a in b.parents or b in a.parents:
        return True
    try:
        a_stat = os.stat(a)
        b_stat = os.stat(b)
    except OSError:
        return False
    if stat.S_ISREG(a_stat.st_mode) and stat.S_ISREG(b_stat.st_mode):
        return a_stat.st_dev == b_stat.st_dev and a_stat.st_ino == b_stat.st_ino
    return False


def _delete_locks_in_tx(
    conn: Any,
    scopes: list[str],
    holder_id: str = "",
) -> None:
    """单事务内删除本操作的全部资源锁（幂等：无锁/无 holder 直接返回）。

    holder_id 缺失（回收/标记路径）时从锁行反查；与终态写同事务（seq 245
    P4：绝不再开第二连接）。
    """
    ordered = sorted(
        {str(scope).strip() for scope in scopes if str(scope).strip()}
    )
    if not ordered:
        return
    holder = str(holder_id or "").strip()
    if not holder:
        placeholders = ",".join("?" for _ in ordered)
        row = conn.execute(
            f"SELECT holder_instance FROM resource_locks "
            f"WHERE canonical_scope IN ({placeholders}) LIMIT 1",
            ordered,
        ).fetchone()
        holder = str(row["holder_instance"] or "") if row is not None else ""
        if not holder:
            return
    for scope in ordered:
        conn.execute(
            "DELETE FROM resource_locks "
            "WHERE canonical_scope = ? AND holder_instance = ?",
            (scope, holder),
        )


# LLM: 原 claim 保留列表顺序和重复项，读取按原资源集合语义核验；畸形字段不得清洗成有效身份。
# 函数用途: 判断已保存的资源声明是否正好等于宿主要求，不改写原账。
def _matching_resource_scopes(payload: dict[str, Any], expected: tuple[str, ...]) -> bool:
    actual = payload.get("resource_scopes")
    return (isinstance(actual, list) and all(isinstance(value, str) and value.strip() for value in actual)
            and set(actual) == set(expected))


def _scopes_from_payload(payload: dict[str, Any]) -> list[str]:
    """从 outcome_json 提取 resource_scopes（claim 时落账，mark/settle/reopen 共用）。"""
    raw = payload.get("resource_scopes") or []
    if not isinstance(raw, list):
        return []
    return [str(scope) for scope in raw if str(scope or "").strip()]


def _upsert_mutation_mutating_in_tx(
    conn: Any,
    scope: str,
    *,
    attempt_id: str,
    now: float,
) -> None:
    """单事务内 mutation → MUTATING（INSERT 或无条件接管，语义同 claim）。"""
    existing = conn.execute(
        "SELECT mutation_id FROM resource_mutations WHERE canonical_scope = ?",
        (scope,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO resource_mutations(mutation_id, canonical_scope, version,
                                           state, dirty_reason, attempt_id, updated_at)
            VALUES(?, ?, 0, 'MUTATING', '', ?, ?)
            """,
            (uuid.uuid4().hex, scope, attempt_id, now),
        )
    else:
        # 无条件接管：DIRTY 语义（G.14）是阻止发布/验收/交付，不阻止新写；
        # 锁 UNIQUE 已保证本事务无并发写者，mutation 行只反映「最近一次写
        # 的结果」。条件 WHERE state='STABLE' 会把 settle 失败留下的 DIRTY
        # 当成拒绝新写的理由（f2 sibling 撞 begin_mutation）。
        conn.execute(
            """
            UPDATE resource_mutations
            SET state = 'MUTATING', attempt_id = ?, updated_at = ?
            WHERE mutation_id = ?
            """,
            (attempt_id, now, str(existing["mutation_id"])),
        )


def _mark_mutation_dirty_in_tx(
    conn: Any,
    scope: str,
    *,
    reason: str,
    attempt_id: str,
    now: float,
) -> None:
    """单事务内 mutation → DIRTY（INSERT 或无条件 UPDATE，语义同 repo.mark_mutation_dirty）。"""
    existing = conn.execute(
        "SELECT mutation_id FROM resource_mutations WHERE canonical_scope = ?",
        (scope,),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO resource_mutations(mutation_id, canonical_scope, version,
                                           state, dirty_reason, attempt_id, updated_at)
            VALUES(?, ?, 0, 'DIRTY', ?, ?, ?)
            """,
            (uuid.uuid4().hex, scope, reason, attempt_id, now),
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


def _mark_mutation_stable_in_tx(
    conn: Any,
    scope: str,
    *,
    now: float,
) -> None:
    """单事务内 mutation MUTATING→STABLE（CAS，语义同 repo.mark_mutation_stable）。"""
    updated = conn.execute(
        """
        UPDATE resource_mutations
        SET state = 'STABLE', updated_at = ?
        WHERE canonical_scope = ? AND state = 'MUTATING'
        """,
        (now, scope),
    ).rowcount
    if updated != 1:
        raise RuntimeConflictError(
            f"mark_mutation_stable 失败: {scope} 非 MUTATING"
        )


def _existing_operation_decision(
    conn: Any,
    row: sqlite3.Row,
    request: ToolOperationClaimRequest,
    now: float,
) -> ToolOperationClaim:
    """幂等行决策：终态 → replay；unknown → unknown；占用中 → in_flight/unknown。

    决策里的 UNKNOWN 标记必须在本事务内联（conn 已 BEGIN IMMEDIATE）：开第二
    连接写会撞外层写锁（SQLITE_BUSY，seq 245 P3）。
    """
    record = _record_from_row(row, owner_id=request.owner_id)
    mismatch = _claim_mismatch_reason(record, request)
    if mismatch:
        return ToolOperationClaim("conflict", record, mismatch)
    status = str(row["status"] or "")
    if status in {OP_SUCCEEDED, OP_FAILED, "CANCELLED"}:
        return ToolOperationClaim("replay", record)
    if status == OP_UNKNOWN:
        return ToolOperationClaim("unknown", record, record.unknown_reason)
    if status not in {OP_CLAIMED, OP_EXECUTING}:
        return ToolOperationClaim(
            "conflict", record, f"unsupported persisted status: {status}"
        )
    payload = _outcome_payload(row)
    holder = payload.get("holder") if isinstance(payload.get("holder"), dict) else {}
    holder_id = str(holder.get("holder_id") or "")
    lease = float(payload.get("lease_expires_at") or 0)
    if holder_id == request.holder.holder_id:
        return ToolOperationClaim("in_flight", record)
    if lease > now and _holder_pid_alive(holder):
        return ToolOperationClaim("in_flight", record)
    reason = (
        "operation lease expired before a terminal result was persisted"
        if lease <= now
        else "operation owner process is no longer alive"
    )
    updated = _mark_managed_unknown(conn, row, reason=reason)
    return ToolOperationClaim(
        "unknown", updated if updated is not None else record, reason
    )


def _mark_managed_unknown(
    conn: Any,
    row: sqlite3.Row,
    *,
    reason: str,
) -> ToolOperationRecord | None:
    """同一事务内：行 UNKNOWN（保留 payload）+ 释放本操作锁 + mutation DIRTY。

    seq 248 #5：自动过期路径此前漏了 mutation 声明——行 UNKNOWN + 删锁后
    mutation 仍悬 MUTATING，后续对账无法从 DIRTY 事实恢复。与
    _mark_unknown_single_transaction 同形：三件事同一事务，事务内原子。
    """
    now = time.time()
    payload = _outcome_payload(row)
    payload["reason"] = reason
    payload["unknown_reason"] = reason
    updated = conn.execute(
        """
        UPDATE tool_operations
        SET status = 'UNKNOWN', outcome_json = ?, updated_at = ?
        WHERE operation_id = ? AND settled_at = 0
        """,
        (json.dumps(payload, ensure_ascii=False), now, str(row["operation_id"])),
    ).rowcount
    if updated != 1:
        return None
    scopes = _scopes_from_payload(payload)
    _delete_locks_in_tx(conn, scopes)
    for scope in scopes:
        _mark_mutation_dirty_in_tx(
            conn,
            scope,
            reason=f"lease_expired:{reason[:200]}",
            attempt_id=str(row["attempt_id"] or ""),
            now=now,
        )
    fresh = conn.execute(
        "SELECT * FROM tool_operations WHERE operation_id = ?",
        (str(row["operation_id"]),),
    ).fetchone()
    if fresh is None:
        return None
    return _record_from_row(fresh, owner_id="") or None


def _claim_mismatch_reason(
    record: ToolOperationRecord,
    request: ToolOperationClaimRequest,
) -> str:
    comparisons = (
        ("tool", record.tool, request.tool),
        ("args_hash", record.args_hash, request.args_hash),
        ("idempotency_scope", record.idempotency_scope, request.idempotency_scope),
        (
            "idempotency_namespace",
            record.idempotency_namespace,
            request.idempotency_namespace,
        ),
        ("idempotency_key", record.idempotency_key, request.idempotency_key),
    )
    changed = [name for name, previous, current in comparisons if previous != current]
    return (
        "operation identity was reused with different structured input: "
        + ",".join(changed)
        if changed
        else ""
    )


def _holder_pid_alive(holder: dict[str, Any]) -> bool:
    pid = int(holder.get("pid") or 0)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# LLM: Reconciliation claims use the same host process evidence and a hard lease. A dead local
# holder may be replaced before expiry; cross-host ambiguity remains live until the lease expires.
# 函数用途: 判断 runtime.db 中的未知操作核对租约是否仍有效。
def _managed_reconciliation_claim_is_live(
    marker: dict[str, Any],
    now: float,
) -> bool:
    if float(marker.get("lease_expires_at") or 0) <= now:
        return False
    holder = marker.get("holder")
    if not isinstance(holder, dict):
        return False
    host = str(holder.get("host") or "")
    if host and host != socket.gethostname():
        return True
    return _holder_pid_alive(holder)


# ------------------------------------------------------------------ finish 内部

def _settle_operation_via_repo(
    repo: Any,
    operation_id: str,
    *,
    outcome: str,
    details: dict[str, Any],
) -> None:
    """settle 原语：单连接单事务内联（终态 CAS + 删锁 + mutation STABLE 同 commit）。

    seq 245 P4：settle 先 commit → release_locks 再 commit → 逐 scope
    mark_mutation_stable 再 commit 的三段式在间隙崩溃会留下「SUCCEEDED 但锁
    仍在 / mutation 悬 MUTATING」；本函数把三件事放进同一个 BEGIN IMMEDIATE
    事务，任一失败整事务回滚（异常上抛，由 _mark_settle_failure 转
    UNKNOWN+DIRTY 收口）。

    seq 241 d：此函数是故障注入的唯一 OperationStore 边界——签名
    (repo, operation_id, *, outcome, details) 保持，finish 的所有 settle 均
    经此调用；内部不再调 repo.settle_operation（各自开连接会撞外层写锁
    SQLITE_BUSY）。终态 CAS 的 WHERE 语义与 repo.settle_operation 一致
    （settled_at=0 + current attempt pointer + CANCELLED 需 handler 未启动）。
    """
    outcome = str(outcome or "").strip().upper()
    if outcome not in {OP_SUCCEEDED, OP_FAILED, OP_CANCELLED}:
        raise RuntimeConflictError(f"非法 settle 结果: {outcome!r}")
    now = time.time()
    # seq 248 #2/#3：finish 在 details 里注入 CAS 期望（_holder_id/_expected_generation
    # 内部键，pop 后不进 outcome_json）。epoch 期望不靠 request——claim 时已捕获
    # 进 payload，settle 同事务与当前 agent_runs.workspace_epoch 比较。
    expected_holder = str(details.pop("_holder_id", "") or "")
    expected_generation = details.pop("_expected_generation", None)
    with repo._runtime_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT op.*, ar.workspace_epoch AS _current_epoch
            FROM tool_operations op
            JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id
            WHERE op.operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"operation 不存在: {operation_id}")
        payload = _outcome_payload(row)
        holder = payload.get("holder") if isinstance(payload.get("holder"), dict) else {}
        # seq 253 #1：holder 必须无条件非空且恒等比较——空值不能当「跳过校验」。
        if not expected_holder:
            raise RuntimeConflictError(
                f"settle 拒绝: operation {operation_id} 缺少 holder 标识（不得跳过校验）"
            )
        if str(holder.get("holder_id") or "") != expected_holder:
            raise RuntimeConflictError(
                f"settle 拒绝: operation {operation_id} holder 不符"
                f"(提交 {expected_holder}, 行持有 {holder.get('holder_id') or ''})"
            )
        if (
            expected_generation is not None
            and int(expected_generation) != int(row["tool_operation_generation"])
        ):
            raise RuntimeConflictError(
                f"settle 拒绝: operation {operation_id} 代数不符"
                f"(提交 gen={expected_generation}, "
                f"行 gen={row['tool_operation_generation']})"
            )
        claim_epoch = int(payload.get("workspace_epoch") or 0)
        if claim_epoch != int(row["_current_epoch"]):
            raise RuntimeConflictError(
                f"settle 拒绝: operation {operation_id} workspace_epoch 已翻新"
                f"(claim epoch={claim_epoch}, 当前={row['_current_epoch']})"
            )
        updated = conn.execute(
            """
            UPDATE tool_operations
            SET status = ?, settled_at = ?, outcome_json = ?, updated_at = ?
            WHERE operation_id = ?
              AND settled_at = 0
              AND tool_operation_generation = ?
              AND attempt_id = (
                  SELECT current_attempt_id FROM agent_runs r
                  WHERE r.agent_run_id = tool_operations.agent_run_id)
              AND attempt_generation = (
                  SELECT current_attempt_generation FROM agent_runs r
                  WHERE r.agent_run_id = tool_operations.agent_run_id)
              AND (? != 'CANCELLED' OR handler_started_at = 0)
            """,
            (
                outcome,
                now,
                json.dumps(details or {}, ensure_ascii=False),
                now,
                operation_id,
                int(row["tool_operation_generation"]),
                outcome,
            ),
        ).rowcount
        if updated != 1:
            # 诊断（不构成决策）：区分失败原因，报结构化错误（对齐 repo 语义）。
            op = conn.execute(
                "SELECT status, settled_at, handler_started_at, agent_run_id, "
                "attempt_id, attempt_generation "
                "FROM tool_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if int(op["settled_at"]) != 0:
                raise RuntimeConflictError(
                    f"operation 已 settle,禁止二次 settle: {operation_id}"
                )
            if outcome == OP_CANCELLED and int(op["handler_started_at"]) != 0:
                raise RuntimeConflictError(
                    f"operation {operation_id} 已启动 handler(无法证明零副作用),"
                    f"禁止 CANCELLED,只能 UNKNOWN"
                )
            current = conn.execute(
                "SELECT current_attempt_id, current_attempt_generation "
                "FROM agent_runs WHERE agent_run_id = ?",
                (str(op["agent_run_id"]),),
            ).fetchone()
            raise RuntimeConflictError(
                f"settle 拒绝: operation {operation_id} 已不是 current pointer"
                f"(操作 attempt={op['attempt_id']}/gen={op['attempt_generation']},"
                f"当前={current['current_attempt_id'] if current else '?'}/"
                f"gen={current['current_attempt_generation'] if current else '?'})"
            )
        # 同事务收尾：终态 CAS 成功才删锁 + mutation 终态 STABLE。
        scopes = _scopes_from_payload(_outcome_payload(row))
        _delete_locks_in_tx(conn, scopes)
        for scope in scopes:
            _mark_mutation_stable_in_tx(conn, scope, now=now)
        conn.commit()


def _mark_unknown_single_transaction(
    repo: Any,
    operation_id: str,
    *,
    holder_id: str,
    reason: str,
    expected_generation: int | None = None,
) -> None:
    """finish(UNKNOWN) 单事务内联：行 UNKNOWN + 删锁 + mutation DIRTY 同 commit。

    seq 245 P3：mark_operation_unknown / release_locks / mark_mutation_dirty
    各自开连接——外层 BEGIN IMMEDIATE 未提交时第二连接写撞写锁（SQLITE_BUSY），
    且分开提交在间隙崩溃丢语义；本函数同一事务完成三件事，任一失败整事务
    回滚并上抛（调用方按 settle 失败收口）。payload 保留 claim 元数据
    （resource_scopes 等），reason 并入 unknown_reason 双键。

    seq 248 #2/#3：与 settle 同构的 CAS——holder 必须等于行持有者（伪造
    holder 连 UNKNOWN 都标不了）；expected_generation 非空时须等于行代数
    （settle_failure 善后路径不带期望，仅真 holder 可善后）；claim 捕获的
    workspace_epoch 必须仍等于当前 epoch（bump 后旧操作保持 EXECUTING，
    由 reconcile 重开处理）。
    """
    now = time.time()
    with repo._runtime_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT op.*, ar.workspace_epoch AS _current_epoch
            FROM tool_operations op
            JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id
            WHERE op.operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"operation 不存在: {operation_id}")
        payload = _outcome_payload(row)
        holder = payload.get("holder") if isinstance(payload.get("holder"), dict) else {}
        if str(holder.get("holder_id") or "") != str(holder_id or ""):
            raise RuntimeConflictError(
                f"UNKNOWN 标记拒绝: operation {operation_id} holder 不符"
                f"(提交 {holder_id}, 行持有 {holder.get('holder_id') or ''})"
            )
        if (
            expected_generation is not None
            and int(expected_generation) != int(row["tool_operation_generation"])
        ):
            raise RuntimeConflictError(
                f"UNKNOWN 标记拒绝: operation {operation_id} 代数不符"
                f"(提交 gen={expected_generation}, "
                f"行 gen={row['tool_operation_generation']})"
            )
        claim_epoch = int(payload.get("workspace_epoch") or 0)
        if claim_epoch != int(row["_current_epoch"]):
            raise RuntimeConflictError(
                f"UNKNOWN 标记拒绝: operation {operation_id} workspace_epoch "
                f"已翻新 (claim epoch={claim_epoch}, 当前={row['_current_epoch']})"
            )
        payload["reason"] = reason
        payload["unknown_reason"] = reason
        updated = conn.execute(
            """
            UPDATE tool_operations
            SET status = 'UNKNOWN', outcome_json = ?, updated_at = ?
            WHERE operation_id = ? AND settled_at = 0
            """,
            (json.dumps(payload, ensure_ascii=False), now, operation_id),
        ).rowcount
        if updated != 1:
            raise RuntimeConflictError(
                f"operation 已 settled,禁止标记 UNKNOWN: {operation_id}"
            )
        scopes = _scopes_from_payload(payload)
        _delete_locks_in_tx(conn, scopes, holder_id=holder_id)
        for scope in scopes:
            _mark_mutation_dirty_in_tx(
                conn,
                scope,
                reason=f"settle_failed:{reason[:200]}",
                attempt_id=str(row["attempt_id"] or ""),
                now=now,
            )
        conn.commit()

def _completion_payload(repo: Any, request: ToolOperationCompletionRequest) -> dict[str, Any]:
    """settle 前重建 outcome_json：保留 claim 元数据 + 写入终态结果。"""
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT outcome_json FROM tool_operations WHERE operation_id = ?",
            (request.operation_id,),
        ).fetchone()
    payload = _json_loads(row["outcome_json"]) if row is not None else {}
    payload.update(
        {
            "schema": _OUTCOME_SCHEMA,
            "result": _json_payload(request.result),
            "error_code": str(request.error_code or ""),
            "unknown_reason": (
                str(request.unknown_reason or "")
                if request.status == TOOL_OPERATION_UNKNOWN
                else ""
            ),
        }
    )
    return payload


def _mark_settle_failure(
    repo: Any,
    operation_id: str,
    *,
    holder_id: str,
    reason: str,
    expected_generation: int | None = None,
) -> None:
    """settle 持久化故障 → 行 UNKNOWN + 删锁 + mutation DIRTY 单事务。

    与 _mark_unknown_single_transaction 同一事务形状，但标记本身尽力而为：
    标记失败不替换 settle 失败的权威事实（异常继续上抛，coordinator 按
    unknown 收口；滞留 EXECUTING 的行由下次 reconcile 处理）。seq 253 #2：
    只有当前 holder/current generation 的真实持久化故障才可 UNKNOWN——
    expected_generation 透传，代数对不上的旧请求不改写当前权威行。
    """
    try:
        _mark_unknown_single_transaction(
            repo,
            operation_id,
            holder_id=holder_id,
            reason=reason,
            expected_generation=expected_generation,
        )
    except (KeyError, RuntimeConflictError, sqlite3.Error, OSError):
        return


# ------------------------------------------------------------------ payload 构造

def _claim_payload(
    request: ToolOperationClaimRequest,
    *,
    workspace_epoch: int = 0,
) -> dict[str, Any]:
    return {
        "schema": _OUTCOME_SCHEMA,
        "holder": _holder_dict(request.holder),
        "lease_expires_at": float(request.lease_expires_at),
        "args_hash": str(request.args_hash or ""),
        "idempotency_key": str(request.idempotency_key or ""),
        "idempotency_scope": str(request.idempotency_scope or ""),
        "idempotency_namespace": str(request.idempotency_namespace or ""),
        "task_id": str(request.task_id or ""),
        # seq 248 #3：claim 时把 workspace_epoch 捕获进操作权威行（此前只进
        # resource_locks）；settle/UNKNOWN 同事务比较捕获值与当前 epoch，
        # bump（新快照开始）后旧操作 settle 一律拒绝。
        "workspace_epoch": int(workspace_epoch or 0),
        "resource_scopes": [
            str(scope).strip() for scope in request.resource_scopes if str(scope).strip()
        ],
        "result": {},
        "error_code": "",
        "unknown_reason": "",
    }


def _holder_dict(holder: object) -> dict[str, Any]:
    return {
        "holder_id": str(getattr(holder, "holder_id", "") or ""),
        "host": str(getattr(holder, "host", "") or ""),
        "pid": int(getattr(holder, "pid", 0) or 0),
        "process_start_token": str(getattr(holder, "process_start_token", "") or ""),
    }


def _json_payload(value: object) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _json_loads(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _outcome_payload(row: sqlite3.Row) -> dict[str, Any]:
    return _json_loads(row["outcome_json"])


# LLM: 精确结果查询只接受原 TEXT JSON；规范 object/schema 校验与数据库身份查询分开，坏账不补空值，联测取消后的两种查询。
# 函数用途: 解码一份持久工具结果，不读写数据库；损坏、过深、非文本和未知版本统一返回原账本错误类型。
def _checked_operation_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ToolOperationStateError("原工具操作结果记录必须是 JSON 文本")
    try:
        payload = load_strict_json(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ToolOperationStateError("原工具操作结果记录损坏") from exc
    if not isinstance(payload, dict) or payload.get("schema", _OUTCOME_SCHEMA) != _OUTCOME_SCHEMA:
        raise ToolOperationStateError("原工具操作结果记录格式或版本无效")
    return payload


# ------------------------------------------------------------------ record 映射

def _operation_record(
    repo: Any,
    operation_id: str,
    *,
    owner_id: str,
    run_id: str,
    task_id: str,
) -> ToolOperationRecord:
    if repo is None:
        raise AuthorityContextMissing("MANAGED run 无权威库（repo 缺失）")
    with repo._runtime_connection() as conn:
        row = conn.execute(
            """
            SELECT op.*, ar.run_id AS _run_id
            FROM tool_operations op
            JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id
            WHERE op.operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
    if row is None:
        raise ToolOperationStateError(
            f"tool operation claim was not persisted: {operation_id}"
        )
    return _record_from_row(
        row,
        owner_id=owner_id,
        run_id=run_id or str(row["_run_id"] or ""),
        task_id=task_id,
    )


def _record_from_row(
    row: sqlite3.Row,
    *,
    owner_id: str,
    run_id: str = "",
    task_id: str = "",
) -> ToolOperationRecord:
    payload = _outcome_payload(row)
    holder = payload.get("holder") if isinstance(payload.get("holder"), dict) else {}
    status = str(row["status"] or "")
    return ToolOperationRecord(
        owner_id=str(owner_id or ""),
        run_id=str(run_id or ""),
        task_id=str(task_id or "") or str(payload.get("task_id") or ""),
        operation_id=str(row["operation_id"]),
        tool=str(row["operation_type"] or ""),
        args_hash=str(payload.get("args_hash") or ""),
        idempotency_key=str(payload.get("idempotency_key") or ""),
        idempotency_scope=str(payload.get("idempotency_scope") or ""),
        idempotency_namespace=str(payload.get("idempotency_namespace") or ""),
        status=_STATUS_TO_RUNTIME.get(status, TOOL_OPERATION_UNKNOWN),
        holder_id=str(holder.get("holder_id") or ""),
        holder_host=str(holder.get("host") or ""),
        holder_pid=int(holder.get("pid") or 0),
        holder_process_start_token=str(holder.get("process_start_token") or ""),
        generation=int(row["tool_operation_generation"] or 0),
        lease_expires_at=float(payload.get("lease_expires_at") or 0),
        result=_json_payload(payload.get("result") or {}),
        result_ref=str(payload.get("result_ref") or ""),
        error_code=str(payload.get("error_code") or ""),
        unknown_reason=str(payload.get("unknown_reason") or ""),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        completed_at=float(row["settled_at"] or 0),
    )


__all__ = [
    "AuthorityContextMissing",
    "ManagedOperationStore",
]
