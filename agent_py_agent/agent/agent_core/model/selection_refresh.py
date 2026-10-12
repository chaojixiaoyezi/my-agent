# LLM: 显式选模只在原工具循环的请求准备边界采用；复用 ContextVar 依赖、工具协议及原历史，禁止重放工具或重建执行身份。
# 模块用途: 缩短运行中切换模型的等待边界，并将选择版本关联到实际请求配置。
from __future__ import annotations

from dataclasses import replace

from ...settings.model_profiles import (
    ModelProfileError,
    model_profile_generation,
    selected_model_config,
)
from ...settings.model_scope import (
    activate_model_dependencies,
    current_model_selection,
    model_dependencies_scope,
    model_dependency_lifetime_active,
    model_selection_snapshot,
    prepare_model_dependencies,
)
from ...settings.thread_model_selection import _require_owner
from ..native_tool_protocol import select_tool_protocol


# LLM: 调用者已收齐当前响应和已开始工具；提交过的同请求恢复重试不得调用。只读原选择，准备失败不发旧模型请求。
# 函数用途: 在下一请求装配前采用最新显式选择，保留 task/run/attempt、工具记录、权限和用户输入。
def refresh_selected_model(agent: object, params: object):
    current = current_model_selection(agent)
    if current is None or current.selection_source == "scheduler_fixed" or not model_dependency_lifetime_active(agent):
        return params
    attrs = getattr(params, "task_attributes", None) or {}
    if (getattr(params, "context_scope", "") == "task_local"
            or not getattr(params, "consume_pending_turn_input", True)
            or attrs.get("conversation_thread_id") != current.thread_id):
        return params
    for _ in range(4):
        source_thread_id = current.selection_thread_id or current.thread_id
        thread = agent.conversation_store.threads.load(source_thread_id)
        if thread is None:
            raise ModelProfileError("当前模型选择所属会话已不存在。")
        _require_owner(agent, thread)
        if thread.model_selection_last_explicit_revision <= current.explicit_revision:
            return params
        generation = model_profile_generation(agent, thread.model_profile_id)
        config = selected_model_config(agent, profile_id=thread.model_profile_id)
        if model_profile_generation(agent, thread.model_profile_id) != generation:
            continue
        dependencies = prepare_model_dependencies(agent, config)
        if dependencies is None:
            raise ModelProfileError("当前执行入口不能采用新模型依赖。")
        with model_dependencies_scope(agent, dependencies):
            protocol = select_tool_protocol(agent, run_id=params.run_id, request_id=params.request_id)
            from .selection_history import prepare_model_selection_history

            execution_thread = (thread if thread.thread_id == current.thread_id else
                                agent.conversation_store.threads.load(current.thread_id))
            if execution_thread is None:
                raise ModelProfileError("当前执行会话已不存在。")
            _require_owner(agent, execution_thread)
            candidate = prepare_model_selection_history(agent, params, execution_thread)
            candidate = replace(candidate, tool_protocol_snapshot=protocol)
        latest = agent.conversation_store.threads.load(source_thread_id)
        if latest is None:
            raise ModelProfileError("当前模型选择所属会话已不存在。")
        _require_owner(agent, latest)
        if model_profile_generation(agent, thread.model_profile_id) != generation:
            continue
        if (latest.model_profile_id, latest.model_selection_revision) != (thread.model_profile_id, thread.model_selection_revision):
            continue
        selection = replace(model_selection_snapshot(thread, config), thread_id=current.thread_id,
                            selection_thread_id=current.selection_thread_id, selection_source=current.selection_source,
                            scheduler_run_id=current.scheduler_run_id)
        dependencies = replace(dependencies, selection=selection)
        activate_model_dependencies(agent, dependencies, thread_id=current.thread_id)
        from .context_pressure import invalidate_provider_context_observation
        from .selection_history import commit_model_selection_history

        commit_model_selection_history(params, candidate)
        from ...settings.reasoning_effort import clear_next_call_reasoning_level

        clear_next_call_reasoning_level(params)
        invalidate_provider_context_observation(params)
        return params
    raise ModelProfileError("模型选择连续变化，尚未形成可采用的请求快照。")
