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
    replace_recovery_history,
)
from .agent_core.tool_request_projection import project_tool_loop_request
from .conversation import history_projection
from .conversation.compact_guard import ConversationCompactError
from .conversation.native_history import provider_history_messages_from_rows
from .gateway_parts import request_binding, request_context, request_prompt


# LLM: 只由原overflow入口调用；作用域绑定准确agent/request/thread，摘要和child不能消费Gateway来源。
# 函数用途: 为一次Gateway恢复组装原历史投影和成功边界回调，公共恢复器负责实际提交。
def prepare_gateway_compact_recovery(context, conversation):
    from .gateway_parts.request_execution import _publish_gateway_compact_boundary

    # LLM: 匹配结构化身份，不从提示正文认领来源，task_local保持独立。
    # 函数用途: 把完整恢复限制到当前Gateway请求。
    def matches(params):
        return (params.request_id == context.request_id and params.context_scope != "task_local"
                and (params.task_attributes or {}).get("conversation_thread_id") == conversation.thread_id)

    return PreparedCompactRecovery(
        context.agent, conversation.compact_source, conversation, matches,
        partial(_project_recovery_candidate, context, conversation), exclude_request_id=context.request_id,
        progress_callback=request_context._gateway_compact_progress_callback(
            context.on_chunk, store=context.agent.conversation_store, thread=conversation.compact_source.thread,
        ), on_commit=partial(_publish_gateway_compact_boundary, context.on_chunk),
    )


# LLM: 只重投影宿主已拥有的会话注入位置和历史；Goal/记忆/工作区等值固定，正文不得用于识别来源或授权。
# 函数用途: 生成候选的完整下一请求和对应参数，所有更改都在副本，成功 CAS 前不安装。
def _project_recovery_candidate(context, conversation, params, frozen, view):
    if not view.is_candidate:
        return CompactRecoveryMaterial(conversation, params, frozen, project_tool_loop_request(frozen))
    work_scope = request_binding.gateway_message_work_scope(context.request)
    rows = history_projection.conversation_history_rows(
        context.agent, view.thread_id, context.request_id, [], rows=view.messages,
        token_budget=view.history_token_budget, work_scope=work_scope,
    )
    root = str(getattr(getattr(context.agent, "home_paths", None), "owner_compact_dir", "") or "")
    if not root:
        raise ConversationCompactError("缺少原 Compact 证据地址", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    candidate = replace(
        conversation, compact_summary=view.summary, compact_generation=view.compact_generation,
        compact_operation_evidence=deepcopy(view.operation_evidence),
        recent_operation_evidence=deepcopy(view.recent_operation_evidence),
        compact_operation_evidence_ref=str(Path(root) / "conversations" / f"{view.thread_id}.jsonl"),
        history=tuple((row.role, row.content) for row in rows),
        canonical_history_messages=provider_history_messages_from_rows(rows), compact_source=None,
    )
    seed = request_prompt.gateway_conversation_history_seed(candidate, work_scope=work_scope)
    candidate_params, prepared = replace_recovery_history(
        params, frozen, history_seed=seed, injection_index=len(context.request.get("inject", [])),
        injection=request_prompt._conversation_prompt_section(candidate, work_scope=work_scope, include_transcript=False),
    )
    return CompactRecoveryMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))
