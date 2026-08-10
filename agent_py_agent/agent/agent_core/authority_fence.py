"""F7：权威 fence 主链接线（3.txt F.6：handler 前/settle 统一校验）。

R2 的 fence 机制（create_tool_operation / verify_fence /
mark_operation_executing / settle_operation）原本只有测试调用 ——
旁路原型。本模块把它们接进主链工具执行路径（execute_traced_tool_call）：

- authority_context：确保 run 的权威链（TaskRun→AgentRun→AgentAttempt）
  存在，缺则就地登记（A.4：一次 TaskRun 一棵 tree；run_id 复用即复用链）。
- open_authority_operation：每次工具调用建权威操作行 + handler 前
  verify_fence（attempt 必须仍是 current pointer）+ mark_operation_executing；
  fence 失配 → 返回 block_reason，调用方必须拒绝执行 handler（fail-closed）。
- close_authority_operation：handler 完成后 settle_operation
  （SUCCEEDED/FAILED，与结果 ok 对应）。

无权威库（无 home 上下文/纯测试）→ 静默跳过（与 subagents.manager 同一
兼容语义）；权威库故障不阻断真实任务（尽力而为，丢记录可以、崩任务不行）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..runtime_db.operations import RuntimeConflictError
from .tool_loop.recovery import runtime_run_scope


def authority_context(agent: object, params: object) -> tuple[Any, str, str] | None:
    """返回 (repo, agent_run_id, attempt_id)；无权威库/故障 → None（跳过 fence）。

    run 的权威链已存在 → 复用（不重复建 TaskRun）；不存在 → 就地登记
    （主链 R1 链接线：一次 run 一棵 tree）。
    """
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None or not hasattr(repo, "record_run_creation"):
        return None
    scope = runtime_run_scope(agent, params)
    run_id = str(scope.run_id or "").strip()
    if not run_id:
        return None
    try:
        run_row = repo.agent_run_for_run_id(run_id)
        if run_row is not None:
            return (
                repo,
                str(run_row["agent_run_id"]),
                str(run_row["current_attempt_id"] or ""),
            )
        chain = repo.record_run_creation(
            owner_id=str(scope.owner_id or "") or "local/main",
            goal=run_id,
            conversation_task_id=str(scope.task_id or "").strip(),
            thread_id=str(scope.session_id or "").strip(),
            run_id=run_id,
            role=str(scope.agent_kind or "").strip() or "main_agent",
        )
        return repo, str(chain["agent_run_id"]), str(chain["attempt_id"])
    except (sqlite3.Error, OSError, KeyError, AttributeError, TypeError):
        return None


def open_authority_operation(
    repo: Any,
    *,
    agent_run_id: str,
    attempt_id: str,
    tool_name: str,
) -> tuple[str, str]:
    """建权威操作行 + handler 前 fence 校验 + 置 EXECUTING。

    返回 (operation_id, block_reason)。block_reason 非空 = fence 拒绝执行
    （attempt 已不是 current pointer 等），调用方必须拦截 handler。
    """
    if not agent_run_id or not attempt_id:
        return "", ""
    try:
        op = repo.create_tool_operation(
            agent_run_id=agent_run_id,
            attempt_id=attempt_id,
            operation_type=str(tool_name or "tool"),
        )
        operation_id = str(op["operation_id"])
        repo.verify_fence(
            agent_run_id=agent_run_id,
            attempt_id=attempt_id,
            tool_operation_id=operation_id,
            tool_operation_generation=int(op["tool_operation_generation"]),
        )
        repo.mark_operation_executing(operation_id)
        return operation_id, ""
    except RuntimeConflictError as exc:
        return "", f"authority fence 拒绝执行: {exc}"
    except (sqlite3.Error, OSError, KeyError):
        return "", "authority fence 不可用（权威库故障）"


def close_authority_operation(repo: Any, operation_id: str, *, ok: bool) -> None:
    """handler 完成后 settle 权威操作行（尽力而为，失败不崩任务）。"""
    if not operation_id:
        return
    try:
        repo.settle_operation(
            operation_id,
            outcome="SUCCEEDED" if ok else "FAILED",
            details={"source": "tool_loop"},
        )
    except (sqlite3.Error, OSError, RuntimeConflictError, KeyError):
        return


__all__ = [
    "authority_context",
    "open_authority_operation",
    "close_authority_operation",
]
