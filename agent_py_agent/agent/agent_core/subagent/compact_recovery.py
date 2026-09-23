# LLM: 子代理恢复仅使用自己的canonical来源；摘要/容量/取消/CAS复用公共恢复器，不改父历史或首请求选模权威。
# 模块用途: 在child真实恢复准备后投影其历史和第一个宿主注入片段，再继续同次工具循环。
from __future__ import annotations

from functools import partial

from ...conversation.agent_thread import project_agent_thread_context
from ...conversation.compact_guard import ConversationCompactError
from ..compact_request_recovery import (
    CompactRecoveryMaterial,
    PreparedCompactRecovery,
    project_recovery_compact_context,
    replace_recovery_history,
)
from ..tool_request_projection import project_tool_loop_request


# LLM: 绑定原task/run/attempt与thread；不从正文或父参数推断身份，来源为空时由原active-turn路径处理。
# 函数用途: 给原子代理恢复轮构造公共恢复器，不发网络或改持久状态。
def prepare_subagent_compact_recovery(agent, current, task, turn, *, progress_callback, interrupt_check):
    # LLM: 只允许当前child的真实参数消费来源，其他局部任务和摘要保持各自路径。
    # 函数用途: 核对子代理、执行轮与独立会话身份。
    def matches(params):
        return (params.context_scope == "task_local" and params.run_id == task.id
                and params.attempt_id == turn.attempt_id
                and (params.task_attributes or {}).get("agent_thread_id") == current.thread_id)

    return PreparedCompactRecovery(
        agent, current.compact_source, current, matches, partial(_project_subagent_candidate, agent, current),
        exclude_request_id=turn.turn_id or turn.attempt_id, progress_callback=progress_callback,
        interrupt_check=interrupt_check,
    )


# LLM: 候选仅修改子代理自己的历史、同scope临时view和已知第0注入位置；其他权限和工具沿原冻结输入。
# 函数用途: 生成与获选摘要绑定的完整下一请求及参数，提交前不修改当前运行对象。
def _project_subagent_candidate(agent, current, params, frozen, view):
    if not view.is_candidate:
        return CompactRecoveryMaterial(current, params, frozen, project_tool_loop_request(frozen))
    candidate_context = project_recovery_compact_context(current.compact_context, view)
    candidate = project_agent_thread_context(agent, view, compact_context=candidate_context)
    if not current.injection:
        raise ConversationCompactError("子代理恢复注入位置未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    candidate_params, prepared = replace_recovery_history(
        params, frozen, history_seed=candidate.history_seed, injection=candidate.injection, injection_index=0,
        compact_context=candidate_context,
    )
    return CompactRecoveryMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))
