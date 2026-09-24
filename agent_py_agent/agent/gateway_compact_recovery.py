# LLM: Gateway只投影自己的历史与注入位置；完整准备、取消、摘要和CAS复用core共享恢复，不另建持久状态。
# 模块用途: 将Gateway的原上下文和边界事件接到完整请求压缩恢复。
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import partial
from pathlib import Path

from .agent_core.compact_request_recovery import (
    CompactRecoveryMaterial,
    PreparedCompactRecovery,
    project_recovery_compact_context,
    replace_recovery_history,
)
from .agent_core.tool_request_projection import project_tool_loop_request
from .conversation.compact_guard import ConversationCompactError
from .gateway_parts import request_binding, request_context, request_prompt


# LLM: 初次自动准备和原overflow入口共用；作用域绑定准确agent/request/thread，摘要和child不能消费Gateway来源。
# 函数用途: 为一次Gateway恢复组装原历史投影和成功边界回调，公共恢复器负责实际提交。
def prepare_gateway_compact_recovery(context, conversation, *, force=True):
    from .gateway_parts.request_execution import _publish_gateway_compact_boundary

    # LLM: 匹配结构化身份，不从提示正文认领来源，task_local保持独立。
    # 函数用途: 把完整恢复限制到当前Gateway请求。
    def matches(params):
        return (params.request_id == context.request_id and params.context_scope != "task_local"
                and (params.task_attributes or {}).get("conversation_thread_id") == conversation.thread_id)

    return PreparedCompactRecovery(
        context.agent, conversation.compact_source, conversation, matches,
        partial(_project_recovery_candidate, context, conversation), exclude_request_id=context.request_id,
        project_active_candidate=partial(
            _project_gateway_active_candidate, (context, conversation),
        ),
        progress_callback=request_context._gateway_compact_progress_callback(
            context.on_chunk, store=context.agent.conversation_store, thread=conversation.compact_source.thread,
        ), on_commit=partial(_publish_gateway_compact_boundary, context.on_chunk), force=force,
    )


# LLM: 候选保留行必须完整投影给容量门，不套展示窗口；只改已有注入位置和同scope视图，CAS前不发布。
# 候选历史与普通准备共用同一只读来源规则，只在发送边界物化。
# 函数用途: 生成候选的完整下一请求和对应参数，所有更改都在副本，成功 CAS 前不安装。
def _project_recovery_candidate(context, conversation, params, frozen, view):
    if not view.is_candidate:
        return CompactRecoveryMaterial(conversation, params, frozen, project_tool_loop_request(frozen))
    work_scope = request_binding.gateway_message_work_scope(context.request)
    history_source = request_context._gateway_history_source(view.messages, context.request_id, work_scope)
    root = str(getattr(getattr(context.agent, "home_paths", None), "owner_compact_dir", "") or "")
    if not root:
        raise ConversationCompactError("缺少原 Compact 证据地址", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    candidate_context = project_recovery_compact_context(conversation.compact_context, view)
    candidate = replace(
        conversation, compact_summary=view.summary, compact_generation=view.compact_generation,
        compact_operation_evidence=deepcopy(view.operation_evidence),
        recent_operation_evidence=deepcopy(view.recent_operation_evidence),
        compact_operation_evidence_ref=(
            str(Path(root) / "conversations" / f"{view.thread_id}.jsonl")
            if candidate_context is None or candidate_context.scope.kind == "thread" else ""
        ),
        history_source=history_source, compact_context=candidate_context, compact_source=None,
    )
    seed = request_prompt.gateway_conversation_history_seed(candidate, work_scope=work_scope)
    candidate_params, prepared = replace_recovery_history(
        params, frozen, history_seed=seed, injection_index=len(context.request.get("inject", [])),
        injection=request_prompt._conversation_prompt_section(candidate, work_scope=work_scope, include_transcript=False),
        compact_context=candidate_context,
    )
    return CompactRecoveryMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))


# LLM: 绑定原host和scope/view；只替换摘要与会话注入，公共层随后按同一来源替换工具IR。
# 函数用途: 空历史但已有活动工具时，以同一候选材料计量并准备提交后的业务请求。
def _project_gateway_active_candidate(binding, params, frozen, summary, retained, generation):
    context, conversation = binding
    application = conversation.compact_context
    if application is None:
        raise ConversationCompactError("缺少原 Compact 应用范围", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    candidate_context = replace(application, view=replace(application.view, summary=summary, generation=generation))
    candidate = replace(
        conversation, compact_summary=summary, compact_generation=generation,
        compact_context=candidate_context, compact_source=None,
    )
    work_scope = request_binding.gateway_message_work_scope(context.request)
    seed = request_prompt.gateway_conversation_history_seed(candidate, work_scope=work_scope)
    candidate_params, prepared = replace_recovery_history(
        params, frozen, history_seed=seed, compact_context=candidate_context,
        injection=request_prompt._conversation_prompt_section(candidate, work_scope=work_scope, include_transcript=False),
        injection_index=len(context.request.get("inject", [])),
    )
    return CompactRecoveryMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))
