# LLM: 完整恢复的活动工具候选只改冻结请求中的结构化交接；真实工具账、审批和预算仍由原运行参数掌权。
# 模块用途: 在已替换历史种子之后纯投影本轮工具摘要，供同一次Compact容量检查与实际发送共用。
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from ..backends.tool_ir import AssistantTurn, CompactionSummary, ToolResult, UserTurn
from ..conversation.compact_guard import ConversationCompactError
from ..conversation.compact_summary_view import AppliedCompactContext
from ..conversation.tool_context_window import native_carried_tool_handoff
from .runtime.conversation_state import (
    conversation_runtime_state_section,
    record_conversation_compact_generation,
)
from .runtime.loop_support import reconstructed_model_tool_context
from .tool_ir_guidance import unforwarded_runtime_guidance
from .tool_request_projection import ToolLoopRequestInput


# LLM: 只认原构造点写入的 source，不识别正文标记；混入真实调用对或缺旧交接时必须拒绝候选。
# 函数用途: 以保留的归档记录重建模型可见交接，并同步IR、工具文本和Compact代次的冻结副本。
def replace_recovery_active_tools(
    params: object,
    frozen: ToolLoopRequestInput,
    *,
    compact_context: AppliedCompactContext,
    retained_records: list[dict[str, object]] | tuple[dict[str, object], ...],
    max_chars: int,
) -> tuple[object, ToolLoopRequestInput]:
    if (
        not isinstance(compact_context, AppliedCompactContext)
        or getattr(params, "compact_context", None) != compact_context
        or frozen.tool_ir_history is None
        or frozen.provider_history_messages is None
        or frozen.tool_context is None
        or type(max_chars) is not int
        or max_chars <= 0
        or not isinstance(retained_records, (list, tuple))
        or any(not isinstance(record, dict) for record in retained_records)
        or type(compact_context.view.summary) is not str
        or not compact_context.view.summary.strip()
        or type(compact_context.view.generation) is not int
        or compact_context.view.generation <= 0
    ):
        raise ConversationCompactError("恢复活动工具输入未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")

    history = list(frozen.tool_ir_history)
    handoff_positions = [
        index for index, item in enumerate(history)
        if isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff"
    ]
    applied_positions = [
        index for index, item in enumerate(history)
        if isinstance(item, CompactionSummary) and item.source == "applied_compact"
    ]
    if (
        len(handoff_positions) != 1
        or len(applied_positions) > 1
        or any(isinstance(item, ToolResult) or (
            isinstance(item, AssistantTurn) and item.tool_calls
        ) for item in history)
    ):
        raise ConversationCompactError("恢复活动工具IR不可替换", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")

    retained = list(retained_records)
    model_tool_context = reconstructed_model_tool_context(
        retained,
        agent=None,
        request_id=str(getattr(params, "request_id", "") or ""),
        run_id=str(getattr(params, "run_id", "") or ""),
        task_id=str(getattr(params, "task_id", "") or ""),
    )
    handoff = native_carried_tool_handoff(
        model_tool_context, retained, max_chars=max_chars,
    )
    # 旧工具正文由保留集重建；原冻结材料中尚未转发的非工具指引沿原seen合同留在候选尾部。
    model_tool_context.extend(unforwarded_runtime_guidance(
        list(frozen.tool_context), set(frozen.forwarded_guidance or ()),
    ))
    needs_ir_summary = getattr(params, "conversation_history_seed", None) is None
    summary_text = str(compact_context.view.summary or "").strip()
    applied = CompactionSummary(
        f"# Earlier Conversation Summary (generation {compact_context.view.generation})\n{summary_text}",
        source="applied_compact",
    ) if needs_ir_summary and summary_text else None

    projected_ir: list[object] = []
    for item in history:
        if isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff":
            if handoff:
                projected_ir.append(CompactionSummary(handoff, source="carried_tool_handoff"))
        elif isinstance(item, CompactionSummary) and item.source == "applied_compact":
            if applied is not None:
                projected_ir.append(applied)
                applied = None
        else:
            projected_ir.append(item)
    if applied is not None:
        insert_at = 1 if projected_ir and isinstance(projected_ir[0], UserTurn) else 0
        projected_ir.insert(insert_at, applied)

    candidate_params = replace(
        params,
        tool_ir_history=deepcopy(projected_ir),
        provider_history_messages=deepcopy(list(frozen.provider_history_messages)),
        tool_context=deepcopy(model_tool_context),
        live_archive_state=deepcopy(params.live_archive_state),
    )
    if not record_conversation_compact_generation(
        candidate_params, compact_context.view.generation, canonical=True,
    ):
        raise ConversationCompactError("恢复Compact代次倒退", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    prepared = replace(
        frozen,
        tool_ir_history=tuple(candidate_params.tool_ir_history),
        provider_history_messages=tuple(candidate_params.provider_history_messages),
        tool_context=tuple(candidate_params.tool_context),
        conversation_state=conversation_runtime_state_section(candidate_params),
    )
    return candidate_params, prepared


__all__ = ["replace_recovery_active_tools"]
