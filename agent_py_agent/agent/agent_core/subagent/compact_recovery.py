# LLM: 子代理恢复仅使用自己的canonical来源；摘要/容量/取消/CAS复用公共恢复器，不改父历史或首请求选模权威。
# 模块用途: 在child真实恢复准备后投影其历史和第一个宿主注入片段，再继续同次工具循环。
from __future__ import annotations

from dataclasses import replace
from functools import partial

from ...conversation.agent_thread import project_agent_thread_context
from ...conversation.compact_guard import ConversationCompactError
from ...conversation.compact_projection import ConversationCompactView
from ..compact_request_recovery import (
    CompactRecoveryMaterial,
    PreparedCompactRecovery,
    project_recovery_compact_context,
    replace_recovery_history,
)
from ..tool_request_projection import project_tool_loop_request


# LLM: 绑定原task/run/attempt与thread；首轮用完整请求自动阈值预检，溢出恢复仍强制提交。
# 函数用途: 给原子代理模型轮构造公共压缩器，不发网络或改持久状态。
def prepare_subagent_compact_recovery(agent, current, task, turn, *, progress_callback, interrupt_check, force=True):
    # LLM: 只允许当前child的真实参数消费来源，其他局部任务和摘要保持各自路径。
    # 函数用途: 核对子代理、执行轮与独立会话身份。
    def matches(params):
        return (params.context_scope == "task_local" and params.run_id == task.id
                and params.attempt_id == turn.attempt_id
                and (params.task_attributes or {}).get("agent_thread_id") == current.thread_id)

    return PreparedCompactRecovery(
        agent, current.compact_source, current, matches, partial(_project_subagent_candidate, agent, current),
        project_active_candidate=partial(
            _project_subagent_active_candidate, (agent, current),
        ),
        exclude_request_id=turn.turn_id or turn.attempt_id, progress_callback=progress_callback,
        interrupt_check=interrupt_check, force=force,
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


# LLM: 绑定原child view，活动候选只生成摘要/seed和宿主注入；公共层负责同源IR替换，不能在此另建分区。
# 函数用途: 把工具交接压缩后的摘要和保留归档投影到已准备的同一子代理模型请求。
def _project_subagent_active_candidate(binding, params, frozen, summary, retained, generation):
    agent, current = binding
    application = current.compact_context
    source = current.compact_source
    if application is None or source is None:
        raise ConversationCompactError("子代理活动恢复范围缺失", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    candidate_context = replace(application, view=replace(application.view, summary=summary, generation=generation))
    candidate = project_agent_thread_context(agent, ConversationCompactView(
        current.thread_id, generation, summary, (), candidate_context.view.operation_evidence,
        {}, source.policy.trigger_tokens, True,
    ), compact_context=candidate_context)
    candidate_params, prepared = replace_recovery_history(
        params, frozen, history_seed=candidate.history_seed, injection=candidate.injection,
        injection_index=0, compact_context=candidate_context,
    )
    return CompactRecoveryMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))
