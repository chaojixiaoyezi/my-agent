# LLM: 待办只取原审批/私卡账和当前claim，签名不是批准；原字段、秘密和ref不进入公开结果。
# 模块用途: 为生产交互域冷读取原待办，并把内部真人票据交还原提交服务。
from __future__ import annotations

import hashlib
from functools import partial
from types import SimpleNamespace

from ..conversation.input_dependencies import InputDependencyAccess
from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
from ..subagents.services.persistence.service import load_subagent_task
from ..user_space.interaction_refs import verify_interaction_reference
from .contracts import resolve_read_context
from .errors import HostAPIError
from .gateway_runtime import runtime_rows
from .interactions import approval_interaction, respond_approval
from .schemas import thaw


# LLM: 只能借用已存在且owner完全相同的Agent来核其活动claim，不触发pool创建，不继承其执行器。
# 函数用途: 找到原审批所需的当前已加载宿主。
def loaded_owner_agent(services, context):
    agent = services.resources.server.agent
    home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    return agent if home == context.owner_home.home_dir else None


# LLM: 冷输入读取只使用原Store和子任务reader；没有quota/upsert，因此这个对象不能用来写。
# 函数用途: 建立原输入依赖的只读访问口。
def read_inputs(services, context):
    paths = services.resources.runtime_paths(context)
    return InputDependencyAccess(services.resources.store_for(context), context.identity, None,
                                partial(load_subagent_task, paths["subagent_workspace"]), None)


# LLM: MAC、原owner和原输入current都需通过；过期卡不是活待办，坏签名不泄漏其原引用。
# 函数用途: 从原私卡有界读取当前线程的安全内部交互投影。
def pending_inputs(services, authorized, context, thread_id):
    import time

    from ..gateway_parts.private_interaction_service import read_plugin_pending_input
    home = context.owner_home
    rows = runtime_rows(home, home.home_dir.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS))
    inputs, result = read_inputs(services, context), []
    for row in rows:
        kind = row.get("kind")
        if kind not in {"task_credential", "model_configuration", "feishu_oauth"} or row.get("expires_at", 0) <= time.time():
            continue
        reference = row.get("reference") or row.get("state")
        try:
            payload = verify_interaction_reference(home.root, reference, channel=row["channel"], actor_id=row["actor_id"])
        except (ValueError, KeyError, TypeError):
            raise HostAPIError("STORE_UNAVAILABLE", 503) from None
        presentation = "presentation-" + hashlib.sha256(reference.encode()).hexdigest()[:40]
        item = read_plugin_pending_input(home.home_dir, inputs, payload, presentation_ref=presentation)
        if item is not None and item["thread_id"] == thread_id:
            result.append({**item, "payload": payload})
    return result


# LLM: 原TaskLink确定审批根；无加载主体且确有审批记录时无法核当前claim，应明确不可用而非伪空。
# 函数用途: 汇合准确线程的原审批和原输入元数据。
def pending_interactions(services, authorized, thread_id):
    from ..conversation.agent_tool_approval import list_pending_agent_tool_approvals
    context = resolve_read_context(authorized)
    store = services.resources.store_for(context)
    tasks, errors = store.tasks.list_report(thread_id)
    if errors:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    result = pending_inputs(services, authorized, context, thread_id)
    agent = loaded_owner_agent(services, context)
    for task in tasks:
        if not services.allows(authorized, "task", task.task_id):
            continue
        if agent is None:
            if runtime_rows(context.owner_home, store.storage.root / "subagent_tool_approvals" / task.task_id):
                raise HostAPIError("HOST_NOT_READY", 503)
            continue
        for pending in list_pending_agent_tool_approvals(agent, root_task_id=task.task_id):
            reference = "approval-" + hashlib.sha256(str(pending["request"]["permission_id"]).encode()).hexdigest()[:40]
            result.append(approval_interaction(agent, pending, reference))
    return result


# LLM: 公开插件调用不能直接进入本口；只有ResponseTickets的已认证冻结HumanResponse可到这里。
# 函数用途: 提交当前owner原审批或私卡的准确真人决定。
def respond_interaction(services, authorized, ticket):
    context = resolve_read_context(authorized)
    agent = loaded_owner_agent(services, context)
    if agent is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    if ticket.interaction["kind"] == "tool_approval":
        return respond_approval(services, agent, authorized, ticket)
    from ..conversation.input_dependencies import input_dependency_owner_resources
    from ..gateway_parts.private_interaction_service import execute_plugin_input
    from ..user_space.owner_resolver import home_paths_with_owner
    resources = services.resources
    paths = resources.runtime_paths(context)
    inputs, repo = input_dependency_owner_resources(resources.store_for(context), context.identity,
        owner_home=context.owner_home.home_dir, subagent_workspace=paths["subagent_workspace"], commit=None, blocking=True)
    from ..capability.runtime_config_reload import deployment_capability_config
    native = SimpleNamespace(base=agent, inputs=inputs, home_paths=home_paths_with_owner(agent.home_paths, context.owner_home),
        capability=deployment_capability_config(agent.home_paths))

    # LLM: guard由原卡锁内调用，不能在提交前假装CAS已经通过。
    # 函数用途: 在原写门内再次核当前来源及原卡版本。
    def guard(row):
        from .interactions import approval_revision
        services.revalidate(authorized)
        if approval_revision(row) != ticket.revision:
            raise HostAPIError("REVISION_CONFLICT", 409)

    try:
        execute_plugin_input(native, thaw(ticket.interaction["payload"]), thaw(ticket.decision_payload),
                             thaw(ticket.human_identity), guard)
        return "cancelled" if ticket.decision_payload.get("decision") == "cancel" else "submitted"
    finally:
        repo.close()
