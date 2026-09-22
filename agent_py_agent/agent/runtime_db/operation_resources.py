# LLM: 这是原 operation 的只读精确资源准入，不登记新租约/资源账；同次读取核对原 holder、代数、运行权及全部锁。
# 模块用途: 防止准备器在原操作已取消、被接管或丢锁后继续创建和使用宿主资源。

from __future__ import annotations

import time

from .operations import RuntimeConflictError


# LLM: claim 必须是执行时冻结的原记录，不能每次读取当前代次补权；本函数只读，不领取、续租或恢复 UNKNOWN。
# 函数用途: 在同一个数据库读事务中确认指定操作仍持有已声明资源及有效执行身份。
def require_claimed_resources(repo, request, claim, scopes: tuple[str, ...]) -> None:
    from .managed_operation_store import _checked_operation_payload, _require_running_authority

    if (repo is None or not scopes or len(set(scopes)) != len(scopes)
            or not claim.holder_id or claim.generation < 1
            or (claim.owner_id, claim.run_id, claim.task_id, claim.operation_id, claim.tool) != (
                request.owner_id, request.run_id, request.task_id, request.operation_id, request.tool_name)):
        raise RuntimeConflictError("原操作资源绑定不完整")
    with repo._runtime_connection() as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT op.*, ar.run_id AS run_id, ar.current_attempt_id, ar.current_attempt_generation, "
            "ar.workspace_epoch AS current_epoch, ar.status AS agent_run_status, at.status AS attempt_status, "
            "t.owner_id AS owner_id, t.task_id AS task_id "
            "FROM tool_operations op JOIN agent_runs ar ON ar.agent_run_id = op.agent_run_id "
            "JOIN agent_attempts at ON at.attempt_id = op.attempt_id AND at.agent_run_id = ar.agent_run_id "
            "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id JOIN tasks t ON t.task_id = tr.task_id "
            "WHERE op.operation_id = ?", (request.operation_id,),
        ).fetchone()
        if row is None:
            raise RuntimeConflictError("原操作尚未领取")
        payload = _checked_operation_payload(row["outcome_json"])
        holder = payload.get("holder", {})
        if (row["owner_id"] != request.owner_id or row["run_id"] != request.run_id
                or row["task_id"] != request.task_id or not request.attempt_id
                or row["attempt_id"] != request.attempt_id or row["current_attempt_id"] != request.attempt_id
                or row["attempt_generation"] != row["current_attempt_generation"]
                or row["operation_type"] != request.tool_name or row["status"] != "EXECUTING"
                or row["settled_at"] != 0 or row["handler_started_at"] <= 0
                or row["tool_operation_generation"] != claim.generation
                or not isinstance(holder, dict) or holder.get("holder_id") != claim.holder_id
                or holder.get("host") != claim.holder_host or holder.get("pid") != claim.holder_pid
                or holder.get("process_start_token") != claim.holder_process_start_token
                or payload.get("args_hash") != claim.args_hash
                or payload.get("workspace_epoch") != row["current_epoch"]
                or not isinstance(payload.get("resource_scopes"), list)
                or any(scope not in payload["resource_scopes"] for scope in scopes)):
            raise RuntimeConflictError("原操作资源执行权已经变化")
        _require_running_authority(row, request.run_id, request.attempt_id)
        now = time.time()
        for scope in scopes:
            lock = conn.execute("SELECT * FROM resource_locks WHERE canonical_scope = ?", (scope,)).fetchone()
            if (lock is None or lock["holder_instance"] != claim.holder_id
                    or lock["pid"] != claim.holder_pid or lock["start_token"] != claim.holder_process_start_token
                    or lock["attempt_id"] != request.attempt_id
                    or lock["attempt_generation"] != row["attempt_generation"]
                    or lock["tool_operation_generation"] != claim.generation
                    or lock["workspace_epoch"] != row["current_epoch"] or lock["lease_expires_at"] <= now):
                raise RuntimeConflictError("原操作资源锁已经失效")
