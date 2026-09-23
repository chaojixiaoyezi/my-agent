# LLM: 后台只投影已准备的历史和上下文；完整容量、取消与提交沿公共恢复器，不重跑进度/任务准备。
# 模块用途: 在后台真实恢复轮准备完成后，用同一候选计量并发送原请求。
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial

from ..agent_core.compact_request_recovery import (
    CompactRecoveryMaterial,
    PreparedCompactRecovery,
    project_recovery_compact_context,
    replace_recovery_history,
)
from ..agent_core.tool_request_projection import project_tool_loop_request
from .background_compact_context import apply_background_compact_context
from .background_context import PreparedBackgroundContext, render_background_context
from .background_history_seed import BackgroundHistorySeedResult, project_background_history_seed
from .compact_summary_view import AppliedCompactContext


# LLM: 本结构只持有本次准备，不保存第二份会话或范围权威；CAS后由公共恢复器回填实际应用view。
# 类用途: 把后台候选历史与已冻结注入材料共同交回同次执行。
@dataclass(frozen=True)
class BackgroundRecoveryState:
    history: BackgroundHistorySeedResult
    context: PreparedBackgroundContext
    compact_generation: int
    compact_context: AppliedCompactContext


# LLM: 首次与恢复共用同一宿主身份和来源；force=False仅按原完整请求阈值裁决，force=True仍由overflow要求压缩。
# 函数用途: 给后台一次模型尝试安装公共Compact宿主，不执行模型或修改持久状态。
def prepare_background_compact_recovery(agent, history, context, *, request_id, progress_callback, interrupt_check,
                                        force=True):
    source = history.compact_source

    current = BackgroundRecoveryState(history, context, source.thread.compact_generation, history.compact_context)

    # LLM: 显式已应用scope绑定唯一宿主片，摘要子请求和别的线程不能消费其来源。
    # 函数用途: 在原模型安全点判断是否属于此次恢复。
    def matches(params):
        application = getattr(params, "compact_context", None)
        return (params.request_id == request_id and application is not None and application.thread_id == current.compact_context.thread_id
                and application.scope == current.compact_context.scope
                and (params.task_attributes or {}).get("conversation_thread_id") == application.thread_id)

    return PreparedCompactRecovery(
        agent, source, current, matches, partial(_project_background_candidate, agent, current),
        project_active_candidate=partial(_project_background_active_candidate, agent, current),
        progress_callback=progress_callback, interrupt_check=interrupt_check, force=force,
    )


# LLM: 候选只更新原第0注入与seed/view，不读Store、重查任务或执行进度对账；其余已准备输入逐字保留。
# 函数用途: 将后台摘要候选映射为同次准备的完整下一请求，供公共容量门检查。
def _project_background_candidate(agent, current, params, frozen, view):
    if not view.is_candidate:
        return CompactRecoveryMaterial(current, params, frozen, project_tool_loop_request(frozen))
    application = project_recovery_compact_context(current.compact_context, view)
    thread = dict(current.history.projection.thread)
    thread.update(summary=view.summary, compact_generation=view.compact_generation,
                  compact_operation_evidence=view.operation_evidence)
    projection = replace(current.history.projection, thread=thread, compact_context=application)
    seed = project_background_history_seed(agent, projection, view.messages, scope_applied=True)
    history = replace(current.history, seed=seed, projection=projection, compact_context=application, compact_source=None)
    context = apply_background_compact_context(current.context, application, view.compact_generation)
    candidate = BackgroundRecoveryState(history, context, view.compact_generation, application)
    candidate_params, prepared = replace_recovery_history(
        params, frozen, history_seed=seed, compact_context=application,
        injection=render_background_context(context), injection_index=0,
    )
    return CompactRecoveryMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))


# LLM: 本回调只替换历史与注入；公共层负责精确IR分区，窄事件仍无seed，媒体与插话保留。
# 函数用途: 将本次活动摘要投影到后台完整请求，不重跑宿主或工具循环准备。
def _project_background_active_candidate(agent, current, params, frozen, summary, retained, generation):
    application = replace(current.compact_context, view=replace(current.compact_context.view,
                                                               summary=summary, generation=generation))
    seed = current.history.seed
    if seed is not None:
        seed = replace(seed, compact_summary=summary, compact_generation=generation)
    context = apply_background_compact_context(current.context, application, generation)
    history = replace(current.history, seed=seed, compact_context=application, compact_source=None)
    candidate = BackgroundRecoveryState(history, context, generation, application)
    candidate_params, prepared = replace_recovery_history(
        params, frozen, history_seed=seed, compact_context=application,
        injection=render_background_context(context), injection_index=0,
    )
    return CompactRecoveryMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))
