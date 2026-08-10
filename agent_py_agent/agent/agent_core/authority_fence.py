"""F7：权威 fence 主链接线（3.txt F.6：handler 前/settle 统一校验）。

R2 的 fence 机制（create_tool_operation / verify_fence /
mark_operation_executing / settle_operation）原本只有测试调用 ——
旁路原型。本模块把它们接进主链工具执行路径（execute_traced_tool_call）：

- authority_context：确保 run 的权威链（TaskRun→AgentRun→AgentAttempt）
  存在，缺则就地登记（A.4：一次 TaskRun 一棵 tree；run_id 复用即复用链）。
  三态返回：正常 (repo, agent_run_id, attempt_id)；无权威库 → None
  （旁路兼容，legacy/纯测试）；权威库故障 → AUTHORITY_FENCE_FAULT
  （fail-closed 信号，G4：故障绝不停默降级成 None，否则调用方跳过 fence
  继续执行 handler —— 不能证明授权 = 不执行，B.5）。
- open_authority_operation：每次工具调用建权威操作行 + 原子 start 门
  （mark_operation_executing 的 UPDATE 内联 current-pointer 条件，G4 补：
  create 与 EXECUTING 之间无 takeover 竞争窗口）；fence 失配/库故障 →
  返回 block_reason，调用方必须拒绝执行 handler（fail-closed）。
- close_authority_operation：handler 完成后 settle_operation
  （SUCCEEDED/FAILED，与结果 ok 对应；settle 是单事务 CAS，G4 补：
  verify 与 UPDATE 合一，旧 attempt 的操作不得结算）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..runtime_db.operations import RuntimeConflictError
from .tool_loop.recovery import runtime_run_scope

_logger = logging.getLogger(__name__)


class AuthorityFenceFault:
    """权威库故障哨兵类型：authority_context 的故障返回（G4 fail-closed）。"""

    __slots__ = ()


# G4：权威库故障信号。authority_context 返回 None 只有「无权威库（旁路
# 兼容）」一种含义；库查询/登记抛异常必须显式返回此哨兵，调用方看到即
# 拒绝执行 handler，绝不把故障当「可跳过」处理。
AUTHORITY_FENCE_FAULT: AuthorityFenceFault = AuthorityFenceFault()


def authority_context(
    agent: object, params: object
) -> tuple[Any, str, str] | None | AuthorityFenceFault:
    """返回 (repo, agent_run_id, attempt_id)；无权威库 → None（旁路兼容）；
    权威库故障 → AUTHORITY_FENCE_FAULT（fail-closed）。

    run 的权威链已存在 → 复用（不重复建 TaskRun）；不存在 → 就地登记
    （主链 R1 链接线：一次 run 一棵 tree）。
    """
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None or not hasattr(repo, "record_run_creation"):
        return None
    try:
        scope = runtime_run_scope(agent, params)
        run_id = str(scope.run_id or "").strip()
        if not run_id:
            return None
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
    except Exception:
        # G4：权威库故障（查询/登记异常，含运行时异常）→ fail-closed 哨兵，
        # 不是 None —— 调用方必须拒绝 handler，不能静默放行。记录结构化
        # 原因（G4 末段：预期基础设施错误与程序 bug 分类留痕，不无声吞掉）。
        _logger.warning(
            "authority_context 权威库故障(run_id=%s)",
            str(getattr(scope, "run_id", "") or ""),
            exc_info=True,
        )
        return AUTHORITY_FENCE_FAULT


def open_authority_operation(
    repo: Any,
    *,
    agent_run_id: str,
    attempt_id: str,
    tool_name: str,
) -> tuple[str, str]:
    """建权威操作行 + 原子 start 门（fence 校验与置 EXECUTING 单条 UPDATE）。

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
        # G4 补 3：原子 start 门——verify 与置 EXECUTING 合并成单条 UPDATE
        # （mark_operation_executing 带 fence 参数时 current-pointer 条件并入
        # WHERE）。不再先 verify 再 mark 分两步：两步之间被 takeover 换掉
        # current pointer 的话，verify 已过而 mark 却落进死 attempt 里。
        repo.mark_operation_executing(
            operation_id,
            agent_run_id=agent_run_id,
            attempt_id=attempt_id,
            tool_operation_generation=int(op["tool_operation_generation"]),
        )
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
    "AUTHORITY_FENCE_FAULT",
    "AuthorityFenceFault",
    "authority_context",
    "open_authority_operation",
    "close_authority_operation",
]
