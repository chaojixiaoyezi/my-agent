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
#   ORCH01-R5：任何返回之前（含省略 target 或 target 等于自身的提前返回）都先核验会话任务提名；与执行事实冲突或
#   证明不了时直接拒绝，不能借提名读写原父任务自己的目标。原门（requester=已核验的自身）先判；仅当它拒绝时，
#   才用 management_continuity 证明过的定时延续原父任务再走同一个直属下级门。目标修改不转交、不改目标生命周期或
#   版本，拒绝时原样抛出第一道门的 PermissionError。
# 函数用途: 给父代理修改直属下级目标解析精确身份；目标名字、普通正文和用户显示名不能授权。
def goal_tool_target(agent: object, target_run_id: str = "") -> tuple[str, str, str, dict | None]:
    binding = proven_goal_binding(agent)
    target = str(target_run_id or "").strip()
    if not target or target == binding[1]:
        return binding
    if not binding[1]:
        raise PermissionError("当前代理身份不完整，不能修改下级目标。")
    from ..agent_core.orchestration.management_continuity import scheduled_continuation_principal
    from ..subagents.authorization_gate import OperationRequest, authorize_direct_child_operation

    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    requester = binding[1]
    try:
        task = authorize_direct_child_operation(agent.subagents, OperationRequest(
            operation="send_guidance", run_id=target, requester_run_id=requester, requester_owner=owner,
        ))
    except PermissionError as denied:
        continuation, _warning = scheduled_continuation_principal(agent)
        if continuation is None or continuation.run_id == requester:
            raise
        try:
            task = authorize_direct_child_operation(agent.subagents, OperationRequest(
                operation="send_guidance", run_id=target, requester_run_id=continuation.run_id, requester_owner=owner,
            ))
        except PermissionError:
            raise denied from None
    return str(task.agent_thread_id), str(task.id), "", None


# LLM: 子代理和独立命令的身份来自 runner/命令本身，原样返回；会话回合的任务编号只是提名，必须等于
#   authoritative_self_principal 的结果（执行事实无冲突、TaskLink 同线程、读取无错误），否则抛 PermissionError。
#   get/update 经 goal_tool_target、create_goal 直接调用它；其它原始 goal_binding 消费者（记账、提示、self_map）未改。
# 函数用途: 返回已核验自身身份的目标归属。
def proven_goal_binding(agent: object) -> tuple[str, str, str, dict | None]:
    binding = goal_binding(agent)
    attrs = binding[3] if isinstance(binding[3], dict) else {}
    nominated = str(attrs.get("conversation_task_id") or "").strip()
    if current_subagent_run_id(agent) or not binding[1] or binding[1] != nominated:
        return binding
    from ..agent_core.orchestration.management_continuity import authoritative_self_principal

    principal, warning = authoritative_self_principal(agent)
    if warning or principal.run_id != binding[1]:
        raise PermissionError("当前会话任务身份与执行事实不一致，不能读写该目标。")
    return binding
