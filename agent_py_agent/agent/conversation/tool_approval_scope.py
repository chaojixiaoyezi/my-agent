# LLM: 审批归属只读 owner 的任务、线程及执行租约；child 冻结 canonical attempt，main 冻结 claim，恢复换轮不得继承旧确认。
# 模块用途: 为发布、展示和决定审批提供同一身份校验，不创建状态、授予权限或读取模型正文。
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..common.opaque_id import validate_opaque_id
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from .models import THREAD_TASK_LINK_ACTIVE_STATUS


# LLM: 即时投影冻结 child 执行轮/main claim；决定前复读相等才有效，不创建批准源，联测 I4 与主子换轮。
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
    execution_attempt_id: str = ""


# LLM: 子代理先读 canonical run 及活动 attempt；父会话关联不授予身份，旧 pending/decided 不可跨恢复代次。
# 仅权威子代理记录不存在时读取主任务关联，坏账及孤立子线程仍失败；同步核对创建生命周期与主子孙审批回归。
# 函数用途: 只读解析审批的真实执行者，避免把挂在父会话里的孩子误当主代理而丢失审批。
def tool_approval_scope(agent: object, run_id: str) -> ToolApprovalScope:
    selected = validate_opaque_id(run_id, kind="run_id")
    store = getattr(agent, "conversation_store", None)
    if store is None:
        raise RuntimeError("tool approval requires ConversationStore")
    manager = getattr(agent, "subagents", None)
    try:
        task = manager.load(selected) if manager is not None else None
    except FileNotFoundError:
        task = None
    if task is not None:
        return ToolApprovalScope(
            run_id=selected,
            root_task_id=validate_opaque_id(str(task.root_id or ""), kind="root_task_id"),
            thread_id=str(task.agent_thread_id or ""),
            agent_kind="subagent",
            agent_name=str(getattr(task, "name", "") or task.role or selected)[:96],
            active=not task_status_in(str(task.status or ""), SUBAGENT_ENDED_STATUSES),
            execution_attempt_id=str(task.runner_active_attempt_id or ""),
        )
    link, error = store.tasks.load_report(selected)
    if error is not None:
        raise OSError("tool approval task is unreadable")
    if link is None:
        raise FileNotFoundError("tool approval execution is unavailable")
    thread, error = store.threads.load_report(link.thread_id)
    if error is not None or thread is None:
        raise OSError("tool approval thread is unreadable")
    if thread.metadata.get("agent_run_id"):
        raise ValueError("tool approval child record is unavailable")
    return _main_scope(store, link, thread)


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
