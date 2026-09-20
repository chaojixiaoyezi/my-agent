# LLM: 审批归属只读 owner 的任务、线程及执行租约；main 以精确 claim 隔离换轮，child 保持原 canonical run 归属。
# 模块用途: 为发布、展示和决定审批提供同一身份校验，不创建状态、授予权限或读取模型正文。
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..common.opaque_id import validate_opaque_id
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from .models import THREAD_TASK_LINK_ACTIVE_STATUS


# LLM: 该值只是 canonical 状态的即时投影；不能缓存后充当长期授权，决定写回前必须重新读取。
# 类用途: 描述一条审批所属的代理、会话、主任务及当前执行租约。
@dataclass(frozen=True)
class ToolApprovalScope:
    run_id: str
    root_task_id: str
    thread_id: str
    agent_kind: str
    agent_name: str = field(compare=False)
    active: bool = field(compare=False)
    claim_id: str = ""


# LLM: 有任务关联时先验证其线程类型；子线程沿 canonical subagent 读取，不从 ID 前缀或名称猜主子身份。
# 函数用途: 解析一次审批的真实归属，坏账和身份冲突关闭式失败。
def tool_approval_scope(agent: object, run_id: str) -> ToolApprovalScope:
    selected = validate_opaque_id(run_id, kind="run_id")
    store = getattr(agent, "conversation_store", None)
    if store is None:
        raise RuntimeError("tool approval requires ConversationStore")
    link, error = store.tasks.load_report(selected)
    if error is not None:
        raise OSError("tool approval task is unreadable")
    if link is not None:
        thread, error = store.threads.load_report(link.thread_id)
        if error is not None or thread is None:
            raise OSError("tool approval thread is unreadable")
        child_id = str(thread.metadata.get("agent_run_id") or "")
        if not child_id:
            return _main_scope(store, link, thread)
        if child_id != selected:
            raise ValueError("tool approval thread identity conflict")
    manager = getattr(agent, "subagents", None)
    if manager is None:
        raise RuntimeError("subagent manager unavailable")
    task = manager.load(selected)
    return ToolApprovalScope(
        run_id=selected,
        root_task_id=validate_opaque_id(str(task.root_id or ""), kind="root_task_id"),
        thread_id=str(task.agent_thread_id or ""),
        agent_kind="subagent",
        agent_name=str(getattr(task, "name", "") or task.role or selected)[:96],
        active=not task_status_in(str(task.status or ""), SUBAGENT_ENDED_STATUSES),
    )


# LLM: 主代理审批只属于当前线程选定任务的有效 claim；Goal paused 不影响当前执行，旧 claim 不可给新轮授权。
# 函数用途: 从原任务及租约读取主代理审批资格，不以界面活跃文案判断运行中。
def _main_scope(store: object, link: object, thread: object) -> ToolApprovalScope:
    claim, error = store.claims.load_report(thread.thread_id)
    if error is not None:
        raise OSError("tool approval claim is unreadable")
    claim_id = str(claim.get("claim_id") or "")
    active = bool(
        link.status == THREAD_TASK_LINK_ACTIVE_STATUS
        and thread.workspace_task_id == link.task_id
        and claim.get("thread_id") == thread.thread_id
        and claim.get("task_id") == link.task_id
        and claim.get("status") == "running"
        and float(claim.get("expires_at") or 0) > time.time()
        and claim_id
    )
    return ToolApprovalScope(link.task_id, link.task_id, thread.thread_id, "main", "主代理", active, claim_id)


# LLM: 等待期间只比较 canonical 身份与精确 claim；读取失败等同不可继续，不能自动恢复或替换旧审批。
# 函数用途: 判断挂起审批是否仍属于原执行，供中断、换轮和异常后的等待收口。
def tool_approval_scope_is_current(agent: object, expected: ToolApprovalScope) -> bool:
    try:
        current = tool_approval_scope(agent, expected.run_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return bool(current.active and current == expected)
