# LLM: 审批归属只读原owner/任务/claim与显式执行身份；后台main冻结原已发布attempt，cold只校验原claim定位，不从DB补猜代次。
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


# LLM: 即时投影冻结 child 执行轮/main claim；前后台已发布身份均冻结准确 core attempt，复读相等才有效，不创建批准源。
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


# LLM: 前台仍核原typed writer；后台hot核Params/control与原claim发布，cold只读显式claim身份；legacy无发布且无attempt才保原单次空scope。
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
    if not foreground and active:
        attempt_id = _background_main_attempt(agent, link, thread, claim, current)
        active = attempt_id is not None
        attempt_id = attempt_id or ""
    return ToolApprovalScope(link.task_id, link.task_id, thread.thread_id, "main", "主代理", active, claim_id, attempt_id)


# LLM: claim已发布的准确attempt是定位，不是权限；缺发布仅保无实际attempt的旧单次scope，显式冲突/坏schema/DB未知均拒，不查询current补空。
# 函数用途: 给后台主片及cold审批读者投影同一准确代次，不创建或恢复执行。
def _background_main_attempt(agent, link, thread, claim, current):
    if "runtime_authority" not in claim:
        if current is None:
            return ""
        attrs = getattr(current, "task_attributes", None) or {}
        if (getattr(current, "attempt_id", "") or getattr(current, "task_id", "") != link.task_id
                or getattr(current, "run_id", "") != link.task_id
                or not isinstance(attrs, dict)
                or attrs.get("conversation_thread_id", thread.thread_id) != thread.thread_id):
            return None
        return ""
    authority = claim["runtime_authority"]
    keys = ("invocation_run_id", "task_id", "run_id", "agent_run_id", "attempt_id")
    if (type(authority) is not dict or authority.get("schema_version") != "conversation_claim_execution.v1"
            or authority.get("claim_id") != claim["claim_id"] or authority.get("task_id") != link.task_id
            or any(type(authority.get(key)) is not str or not authority[key] for key in keys)):
        return None
    if current is not None and not _background_current_matches(current, thread, authority):
        return None
    if not _foreground_runtime_is_current(agent, thread, authority):
        return None
    return authority["attempt_id"]


# LLM: 原LocalRunControl由core同次发布且仍开放才可用于hot工具；cold不构造替代句柄，JSON/Namespace不能充实际Params或callback。
# 函数用途: 核对当前后台模型片与已领取claim的精确身份，保持原审批会话缓存权限边界。
def _background_current_matches(current, thread, authority) -> bool:
    from ..agent_core.runtime.loop_models import RunParams
    from .local_run_control import LocalRunControl

    if not isinstance(current, RunParams):
        return False
    attrs = current.task_attributes if isinstance(current.task_attributes, dict) else {}
    execution = {key: authority[key] for key in ("task_id", "run_id", "agent_run_id", "attempt_id")}
    control = current.conversation_task_binding_callback
    return bool(current.request_id == authority["invocation_run_id"]
        and current.task_id == execution["task_id"] and current.run_id == execution["run_id"]
        and current.attempt_id == execution["attempt_id"] and attrs.get("conversation_thread_id") == thread.thread_id
        and isinstance(control, LocalRunControl) and control.request_id == current.request_id
        and control.runtime_authority() == execution and LocalRunControl.__call__(control, None))


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


# LLM: 原require_authority之外显式核冻结attempt所属run及generation相等；只读同owner/thread根任务，不用current指针补缺失身份。
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
        attempt = repo.get_attempt(authority["attempt_id"])
        return bool(run is not None and task is not None and attempt is not None
            and attempt["agent_run_id"] == run["agent_run_id"]
            and attempt["attempt_generation"] == run["current_attempt_generation"] and run["run_id"] == authority["run_id"]
            and run["current_attempt_id"] == authority["attempt_id"] and run["role"] == "main"
            and not run["parent_agent_run_id"] and task["owner_id"] == owner_id
            and task["thread_id"] == thread.thread_id and thread.owner_id == owner_id)
    except (OSError, RuntimeError, ValueError, KeyError, AttributeError, TypeError):
        return False


# LLM: 等待期间只比原canonical/claim；actual typed未知同对象穿透，普通读取失败仍False，不能恢复或替换旧审批。
# 函数用途: 判断挂起审批是否仍属于原执行，供中断、换轮和异常后的等待收口。
def tool_approval_scope_is_current(agent: object, expected: ToolApprovalScope) -> bool:
    try:
        current = tool_approval_scope(agent, expected.run_id)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        from ..gateway_parts.private_approval_control import raise_private_approval_transport_error
        raise_private_approval_transport_error(error)
        return False
    return bool(current.active and current == expected)
