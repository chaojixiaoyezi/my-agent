# LLM: 审批归属只读 owner 的任务、线程及执行租约；child 冻结 canonical attempt，main 冻结 claim，恢复换轮不得继承旧确认。
# 模块用途: 为发布、展示和决定审批提供同一身份校验，不创建状态、授予权限或读取模型正文。
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..common.opaque_id import validate_opaque_id
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from .models import THREAD_TASK_LINK_ACTIVE_STATUS


# LLM: 身份记录缺失或不可读属于执行范围故障，不能被凭据工具误报为密钥/磁盘失败；不携带原异常正文。
# 类用途: 让调用方准确区分任务资格不可用和后续私密存储故障。
class ToolApprovalScopeUnavailable(FileNotFoundError):
    pass


# LLM: 即时投影冻结 child 执行轮/main claim；前台同时冻结准确 core attempt，复读相等才有效，不创建批准源。
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
# 仅权威子代理记录不存在时读取主任务关联；当前主运行按原 durable_task_id 映射，坏账及孤立子线程仍失败。
# 函数用途: 只读解析审批的真实执行者，避免把挂在父会话里的孩子误当主代理而丢失审批。
def tool_approval_scope(agent: object, run_id: str) -> ToolApprovalScope:
    selected = validate_opaque_id(run_id, kind="run_id")
    store = getattr(agent, "conversation_store", None)
    if store is None:
        raise ToolApprovalScopeUnavailable("tool approval requires ConversationStore")
    manager = getattr(agent, "subagents", None)
    try:
        task = manager.load(selected) if manager is not None else None
    except FileNotFoundError:
        task = None
    except OSError:
        raise ToolApprovalScopeUnavailable("tool approval execution is unreadable") from None
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
    from ..agent_core.runtime.task_identity import durable_task_id
    from ..runtime_context import current_subagent_run_id

    current = getattr(agent, "_current_run_params", None)
    task_id = (durable_task_id(current) if current is not None and selected == str(current.run_id or "")
               and not current_subagent_run_id(agent) else selected)
    link, error = store.tasks.load_report(task_id)
    if error is not None:
        raise ToolApprovalScopeUnavailable("tool approval task is unreadable")
    if link is None:
        raise ToolApprovalScopeUnavailable("tool approval execution is unavailable")
    thread, error = store.threads.load_report(link.thread_id)
    if error is not None or thread is None:
        raise ToolApprovalScopeUnavailable("tool approval thread is unreadable")
    if thread.metadata.get("agent_run_id"):
        raise ValueError("tool approval child record is unavailable")
    return _main_scope(agent, link, thread)


# LLM: 主代理审批只属于原任务的有效 claim；Gateway 前台车道与长期任务分开核验，不将请求前缀当执行授权。
# 函数用途: 从原任务和当前执行车道读取资格；只读原账，不领取、续租或替换 claim。
def _main_scope(agent: object, link: object, thread: object) -> ToolApprovalScope:
    store = agent.conversation_store
    claim, error = store.claims.load_report(thread.thread_id)
    if error is not None:
        raise ToolApprovalScopeUnavailable("tool approval claim is unreadable")
    claim_id = str(claim.get("claim_id") or "")
    active = bool(
        link.status == THREAD_TASK_LINK_ACTIVE_STATUS
        and claim.get("thread_id") == thread.thread_id
        and claim.get("status") == "running"
        and float(claim.get("expires_at") or 0) > time.time()
        and claim_id
    )
    from ..gateway_parts.request_binding import GatewayTaskBindingWriter

    current = getattr(agent, "_current_run_params", None)
    writer = getattr(current, "conversation_task_binding_callback", None)
    foreground = claim.get("reason") == "gateway_foreground_turn" or isinstance(writer, GatewayTaskBindingWriter)
    if foreground:
        active = active and _foreground_main_is_current(agent, link, thread, claim_id, current, writer)
    else:
        active = active and claim.get("task_id") == link.task_id
    attempt_id = str(getattr(current, "attempt_id", "") or "") if foreground and active else ""
    return ToolApprovalScope(link.task_id, link.task_id, thread.thread_id, "main", "主代理", active, claim_id, attempt_id)


# LLM: 队列路径只能取宿主 typed writer；owner Agent 不从自身 root 猜基础 Gateway，完整目标核验复用原 execution_target。
# 函数用途: 比对本次前台请求的冻结运输代次、车道和 RuntimeDB 身份，不创建任何持久状态。
def _foreground_main_is_current(agent, link, thread, claim_id, current, writer) -> bool:
    from ..gateway_parts.execution_target import read_gateway_execution_target
    from ..gateway_parts.paths import gateway_paths_from_root
    from ..gateway_parts.request_binding import GatewayTaskBindingWriter

    if (not isinstance(writer, GatewayTaskBindingWriter) or current is None
            or str(current.task_id or "") != link.task_id):
        return False
    path = writer.request_path
    if (path.parent.name != "processing" or path.parent.parent.name != "requests"
            or writer.request_id != str(current.request_id or "") or path.name != writer.request_id + ".json"
            or not writer.execution_attempt_id):
        return False
    target = read_gateway_execution_target(gateway_paths_from_root(path.parent.parent.parent),
        agent.conversation_store, thread.thread_id)
    if (target is None or target.kind != "request" or target.turn_id != writer.request_id
            or target.claim_id != claim_id or target.execution_attempt_id != writer.execution_attempt_id):
        return False
    expected = {"task_id": link.task_id, "run_id": str(current.run_id or ""), "attempt_id": str(current.attempt_id or "")}
    if any(not value or target.runtime_authority.get(key) != value for key, value in expected.items()):
        return False
    return _foreground_runtime_is_current(agent, thread, target.runtime_authority)


# LLM: 沿原只读 require_authority 校验 current attempt/generation/status，再核对具体 agent_run 和同 owner/thread 的根任务。
# 函数用途: 拒绝旧 attempt、别的 owner/线程或伪造的运行投影，不懒建或修复权威链。
def _foreground_runtime_is_current(agent, thread, authority) -> bool:
    from ..runtime_db.managed_operation_store import (
        ManagedOperationStore,
        ToolOperationAuthorityRequest,
    )

    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return False
    try:
        owner_id = str(agent.home_paths.owner_id)
        ManagedOperationStore(repo).require_authority(ToolOperationAuthorityRequest(owner_id=owner_id,
            run_id=authority["run_id"], task_id=authority["task_id"], attempt_id=authority["attempt_id"],
            operation_id="", tool_name="tool_approval_scope"))
        run = repo.get_agent_run(str(authority.get("agent_run_id") or ""))
        task = repo.get_task(authority["task_id"])
        return bool(run is not None and task is not None and run["run_id"] == authority["run_id"]
            and run["current_attempt_id"] == authority["attempt_id"] and run["role"] == "main"
            and not run["parent_agent_run_id"] and task["owner_id"] == owner_id
            and task["thread_id"] == thread.thread_id and thread.owner_id == owner_id)
    except (OSError, RuntimeError, ValueError, KeyError, AttributeError, TypeError):
        return False


# LLM: 等待期间只比较 canonical 身份与精确 claim；读取失败等同不可继续，不能自动恢复或替换旧审批。
# 函数用途: 判断挂起审批是否仍属于原执行，供中断、换轮和异常后的等待收口。
def tool_approval_scope_is_current(agent: object, expected: ToolApprovalScope) -> bool:
    try:
        current = tool_approval_scope(agent, expected.run_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return bool(current.active and current == expected)
