# LLM: Goal identity is resolved from the exact runner and canonical agent thread, never parent workspace lineage or prose.
# 模块用途: 主子代理共用目标归属解析；子代理有自己的目标，不能读写父代理目标或给父代理记账。
from __future__ import annotations

from ..agent_core.runtime.task_identity import durable_task_id
from ..runtime_context import current_subagent_run_id


# LLM: 子代理仅使用自身 agent_thread_id/run；独立 cli_run 使用其明确任务身份，不提前建立会话任务链接；无写入。
# 函数用途: 返回当前代理的目标归属；独立命令和会话各读自己的任务编号，身份缺失时不借用父目标。
def goal_binding(agent: object, params: object | None = None) -> tuple[str, str, str, dict | None]:
    if params is None:
        params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return "", "", "", None
    run_id = current_subagent_run_id(agent)
    if run_id:
        thread_id = str(attrs.get("agent_thread_id") or "").strip()
        return thread_id, run_id, "", attrs
    if str(getattr(params, "context_scope", "") or "") == "task_local":
        return "", "", "", None
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    if not task_id and str(getattr(params, "source", "") or "").strip() == "cli_run":
        task_id = durable_task_id(params)
    return (
        str(attrs.get("conversation_thread_id") or "").strip(),
        task_id,
        str(attrs.get("thread_goal_id") or "").strip(),
        attrs,
    )


# LLM: Models may revise only a direct child through existing owner/runtime authorization, not sibling/ancestor targets.
# 函数用途: 给父代理修改直属下级目标解析精确身份；目标名字、普通正文和用户显示名不能授权。
def goal_tool_target(agent: object, target_run_id: str = "") -> tuple[str, str, str, dict | None]:
    binding = goal_binding(agent)
    target = str(target_run_id or "").strip()
    if not target or target == binding[1]:
        return binding
    if not binding[1]:
        raise PermissionError("当前代理身份不完整，不能修改下级目标。")
    from ..subagents.authorization_gate import OperationRequest, authorize_direct_child_operation

    task = authorize_direct_child_operation(agent.subagents, OperationRequest(
        operation="send_guidance", run_id=target, requester_run_id=binding[1],
        requester_owner=str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""),
    ))
    return str(task.agent_thread_id), str(task.id), "", None
