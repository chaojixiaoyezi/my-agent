# LLM: 本模块只准备换模型后的历史副本；canonical transcript、工具归档和 Compact 检查点仍是唯一来源，不执行历史工具。
# 模块用途: 去除旧供应商的推理续接状态，并从原归档恢复新模型不能读取的压缩内容。
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from ...backends.tool_ir import AssistantTurn, CompactionSummary

_PROVIDER_REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking", "responses_reasoning"})


# LLM: 只移除宿主已知的供应商推理块；保留开放类型、正文、媒体和 canonical tool_call/result，不清空执行/输入账本。
# 函数用途: 为新模型复制历史并清理不可跨模型的推理状态，原参数保持不变。
def prepare_model_selection_history(agent: object, params: object, thread: object):
    history = [replace(item, content_blocks=_portable_blocks(item.content_blocks))
               if isinstance(item, AssistantTurn) else item for item in params.tool_ir_history]
    prior = [dict(row, content=_portable_blocks(row.get("content"))) for row in params.provider_history_messages]
    candidate = replace(params, tool_ir_history=history, provider_history_messages=prior,
                        live_archive_state=dict(params.live_archive_state))
    context = getattr(params, "compact_context", None)
    if context is None or not context.view.provider_compaction:
        return candidate
    from ...conversation.compact_remote import provider_compaction_compatible

    if provider_compaction_compatible(agent.backend, context.view.provider_compaction):
        return candidate
    return _restore_portable_context(agent, candidate, thread)


# LLM: 供应商内部块由 type 标记；不能按文本/扩展名猜测，未知块完整保留。
# 函数用途: 去除带原模型签名或密文的思考回放块，复制其余内容。
def _portable_blocks(content: object):
    if not isinstance(content, list):
        return deepcopy(content)
    return [deepcopy(block) for block in content
            if not (isinstance(block, dict) and block.get("type") in _PROVIDER_REASONING_BLOCKS)]


# LLM: 不兼容密文必须从同范围原始来源恢复，不能丢弃后继续；scope 固定，完整归档只作为上下文，不改副作用账。
# 函数用途: 为新后端重载可读检查点和被旧密文覆盖的会话、工具结果，随后交回原容量与压缩链。
def _restore_portable_context(agent: object, params: object, thread: object):
    from ...conversation.compact import load_conversation_compact_source
    from ...conversation.compact_guard import ConversationCompactError
    from ...conversation.history_seed import ConversationHistorySource

    previous = params.compact_context
    seed = params.conversation_history_seed
    if previous.scope.kind != "turn" and (seed is None or not isinstance(seed.source, ConversationHistorySource)):
        raise ConversationCompactError("换模型需要可恢复的原历史来源", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    selector = _scope_selector(previous.scope)
    source = load_conversation_compact_source(
        agent, agent.conversation_store, thread, scope=previous.scope,
        exclude_request_id=params.request_id, selector_factory=selector,
    )
    context = source.compact_context
    if previous.scope.kind != "turn":
        seed = replace(seed, compact_summary=context.view.summary, compact_generation=context.view.generation,
                       source=replace(seed.source, rows=source.messages))
    params = replace(params, compact_context=context, conversation_history_seed=seed)
    params = replace(params, tool_ir_history=_restore_tool_history(agent, params))
    from ..runtime.loop_support import _native_provider_history_messages

    prior = [dict(row, content=_portable_blocks(row.get("content")))
             for row in _native_provider_history_messages(params, include_compact_summary=False)]
    return replace(params, provider_history_messages=prior)


# LLM: 只从已冻结 CompactScope 编译原后台任务选择器，不能重读 task link 扩大任务范围；turn 永不读取 transcript。
# 函数用途: 换模型时沿原线程、独立任务或活动轮范围重载归档。
def _scope_selector(scope):
    if scope.kind == "thread":
        return None
    if scope.kind == "turn":
        return lambda _positions: lambda _row: False
    from ...conversation.background_context import detached_row_selector

    link = {"context_anchor_message_id": scope.anchor_message_id, "created_at": scope.created_at}

    # LLM: 全部位置来自同一次 canonical 消息快照；沿原精确 lineage 和锚点，不以正文识别任务。
    # 函数用途: 将原任务历史筛选适配到存储逐行读取。
    def compile_selector(positions):
        predicate = detached_row_selector(link, frozenset(scope.task_ids), positions)
        return lambda row: predicate({"message_id": row.message_id, "created_at": row.created_at,
                                      "metadata": row.metadata}, positions[row.message_id])
    return compile_selector


# LLM: 恢复缺少的原工具事实与输入，保留已存在的完整工具对；恢复只生成上下文，不执行或重新登记工具。
# 函数用途: 将旧密文覆盖的活动工具结果恢复为可读事实，继续保留用户插话与可读摘要。
def _restore_tool_history(agent: object, params: object) -> list:
    from ...backends.tool_ir import RuntimeFactsTurn, UserTurn
    from ...conversation.active_turn_compact import model_visible_active_turn_tool_calls
    from ...conversation.active_turn_input import merge_active_turn_user_inputs
    from ...conversation.compact_tool_identity import compact_tool_ref_key
    from ..runtime.loop_support import reconstructed_model_tool_context

    history = [item for item in params.tool_ir_history
               if not (isinstance(item, CompactionSummary) and item.source == "applied_compact")]
    refs = {compact_tool_ref_key(call) for item in history if isinstance(item, AssistantTurn) for call in item.tool_calls}
    records = model_visible_active_turn_tool_calls(
        agent, params.task_attributes, params.archive_tool_calls, compact_context=params.compact_context,
    )
    missing = [row for row in records if compact_tool_ref_key(row) not in refs]
    restored = reconstructed_model_tool_context(missing, agent=None)
    view = params.compact_context.view
    prefix = [CompactionSummary(view.summary, source="applied_compact")] if view.summary else []
    if restored:
        prefix.append(RuntimeFactsTurn("\n\n".join(restored), source="model_selection_restored_tools"))
    carried_ids = {key for item in history if isinstance(item, UserTurn) for key in item.input_ids}
    for packet in merge_active_turn_user_inputs(params.active_turn_user_inputs):
        missing_ids = tuple(key for key in packet["input_ids"] if key not in carried_ids)
        if missing_ids:
            prefix.append(UserTurn(packet["text"], input_ids=missing_ids))
    return [*prefix, *history]


# LLM: 仅在依赖准备和选择复核成功后提交内存投影；保留原容器身份供异常收尾和归档回调使用，不改工具执行账。
# 函数用途: 在原循环参数上采用新历史及协议，使切换后出错仍能保存已发生的完整工具事实。
def commit_model_selection_history(params: object, prepared: object) -> None:
    params.tool_ir_history[:] = prepared.tool_ir_history
    params.provider_history_messages[:] = prepared.provider_history_messages
    for key in ("tool_protocol_snapshot", "compact_context", "conversation_history_seed"):
        object.__setattr__(params, key, getattr(prepared, key))
