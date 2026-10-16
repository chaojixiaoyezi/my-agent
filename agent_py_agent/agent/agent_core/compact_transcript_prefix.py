# LLM: 只截同次主投影，不从工具归档拼材料；联合工具只在保护事实之前纳入，保持原IR与覆盖权威。
# 模块用途: 将可容纳的同次主前缀交给摘要；必超容量时在解绑边界释放优化，覆盖仍由原来源负责。
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass

from ..backends.message_adapter import AnthropicMessageAdapter, is_applied_compact_summary
from ..backends.tool_ir import CompactionSummary, RuntimeFactsTurn
from ..conversation.compact_guard import ConversationCompactError
from ..conversation.compact_message_source import CompactMessageSource
from ..conversation.native_history import provider_history_messages_from_rows
from .tool_request_projection import project_tool_loop_request


# LLM: 自动预检还未采样主轮；沿同一历史种子和主摘要构造器冻结其确定前缀，不加入新的人格/运行尾项。
# 函数用途: 为自动preflight保存主历史投影，候选只能在该快照内取连续覆盖终点。
def capture_preflight_prefix(request, surface):
    from ..conversation.compact_remote import provider_compaction_content
    from ..conversation.history_projection import project_history_row
    from ..conversation.history_seed import freeze_history_source
    from .tool_ir_history import applied_compact_summary_item

    source = surface.history_source or freeze_history_source(request.pending, project_row=project_history_row)
    rows = source.projected_rows()
    view = request.compact_context.view if request.compact_context is not None else request.thread
    summary = str(view.summary or '').strip()
    cipher = provider_compaction_content(getattr(view, 'provider_compaction', None))
    generation = getattr(view, 'generation', request.thread.compact_generation)
    applied = [applied_compact_summary_item(summary, generation, cipher)] if summary or cipher else []
    prefix = tuple(AnthropicMessageAdapter().to_provider_messages(applied))
    history = provider_history_messages_from_rows(rows)
    return TranscriptInputPrefix(prefix + history, len(prefix), rows, tuple(row.message_id for row in rows))


# LLM: 只判断同一冻结前缀加空摘要指令的容量下界，不准备模型、不改来源或吞掉错误；后续完整请求精确门仍必须执行。
#   校准在 observed 边界可能向下跳，取原函数在 raw 和未来边界的最小值，不能只用当前点误拒可容纳的优化。
# 函数用途: 在解绑前证明前缀必超原预算时让调用方不保留整份旧历史；不确定或恰好等于预算仍保留。
def transcript_prefix_exceeds_capacity(agent, surface, prefix, calibration):
    from ..conversation.auxiliary_model_call import AuxiliaryModelCallRequest
    from ..conversation.compact_calibration import calibrated_compact_request_tokens
    from ..conversation.compact_provider_surface import conversation_compact_provider_prompt
    from ..conversation.compact_request_budget import (
        compact_cache_surface_budget,
        compact_request_tokens,
    )

    request = AuxiliaryModelCallRequest(
        agent=agent, prompt=conversation_compact_provider_prompt(surface, ""),
        tools=list(surface.tools) if surface.tools is not None else None,
        system_instruction=surface.system_instruction,
    )
    source = prefix(prefix.rows[:len(prefix.source_ids)])
    raw = compact_request_tokens(request, source)
    lower_bound = calibrated_compact_request_tokens(raw, calibration)
    if calibration is not None and raw < calibration.raw_estimated_tokens:
        lower_bound = min(lower_bound, calibrated_compact_request_tokens(calibration.raw_estimated_tokens, calibration))
    return lower_bound > compact_cache_surface_budget(agent)


# LLM: 只读同次可覆盖前缀的块类型和既有能力声明；返回原typed媒体决定，不发探针，不把未声明当不支持。
#   调用方须按实际候选canonical媒体事实复核，不能将全来源决定盲套到未覆盖该媒体的较小候选。
# 函数用途: 在解绑前丢弃明确不可用的媒体前缀，但保留原归档原因供候选沿既有媒体链消费。
def declared_transcript_prefix_media_decision(agent, prefix):
    from ..conversation.compact_media_policy import (
        COMPACT_PREFIX_MEDIA_UNSUPPORTED,
        MEDIA_POLICY_ARCHIVED_REFS,
        CompactMediaDecision,
        declared_prefix_media_unsupported,
    )

    with closing(iter(prefix(prefix.rows[:len(prefix.source_ids)]))) as messages:
        if declared_prefix_media_unsupported(agent, messages):
            return CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, "declared", COMPACT_PREFIX_MEDIA_UNSUPPORTED)
    return None


# LLM: 实际请求只持有主投影的前缀；覆盖行来自同次种子，不持有活动尾部，也不读已解绑参数。
# 类用途: 为候选历史提供可重放的逐项主前缀，范围未知时在摘要前拒绝。
@dataclass(frozen=True)
class TranscriptInputPrefix:
    messages: tuple[dict, ...]
    summary_count: int
    rows: object
    source_ids: tuple[str, ...]
    joint_end: int = 0

    # LLM: 候选必须是来源行的连续前缀；种子只用于确定终点，运输正文始终来自保存的主投影。
    # 函数用途: 返回截至覆盖行的实际主input，不改变任何条目或跳过中段。
    def __call__(self, rows):
        ids = tuple(row.message_id for row in rows)
        if not ids or ids != self.source_ids[:len(ids)]:
            raise ConversationCompactError('transcript覆盖不是主投影前缀', code='COMPACT_SOURCE_CHANGED')
        projected = provider_history_messages_from_rows(self.rows[:len(ids)])
        end = self.summary_count + len(projected)
        if tuple(self.messages[self.summary_count:end]) != projected:
            raise ConversationCompactError('transcript终点与主种子不一致', code='COMPACT_SOURCE_CHANGED')
        if self.joint_end:
            if ids != self.source_ids:
                raise ConversationCompactError('联合覆盖必须包含完整前置历史', code='COMPACT_SOURCE_CHANGED')
            end = self.joint_end
        prefix = self.messages[:end]
        return CompactMessageSource(lambda: iter(deepcopy(prefix)), preserve_prefix=True)


# LLM: 只绑定同次恢复参数和工具分区，不持久化或产生来源身份；减少捕获入口的独立参数面。
# 类用途: 将主参数与已选工具来源作为一个冻结选择传给前缀捕获。
@dataclass(frozen=True)
class TranscriptPrefixSelection:
    params: object
    tool_source: object = None


# LLM: 调用方必须在解绑完整历史前调用；正式摘要先于provider历史，沿同一Adapter数条目，不猜角色或正文。
# 函数用途: 保存完整主投影中可覆盖的旧历史；当前轮事实/事件/工具保持在终点之后。
def capture_transcript_prefix(frozen, source, selection):
    params, tool_source = selection.params, selection.tool_source
    projection = project_tool_loop_request(frozen)
    summary = AnthropicMessageAdapter().to_provider_messages([
        item for item in frozen.tool_ir_history if is_applied_compact_summary(item)
    ])
    base = tuple(summary) + tuple(frozen.provider_history_messages)
    if projection.status != 'ready' or tuple(projection.messages[:len(base)]) != base:
        raise ConversationCompactError('主请求历史边界未知', code='COMPACT_REQUEST_PROJECTION_UNKNOWN')
    seed_source = getattr(getattr(params, 'conversation_history_seed', None), 'source', None)
    rows = seed_source.projected_rows() if seed_source is not None else source.messages
    ids = tuple(row.message_id for row in rows)
    expected_ids = tuple(row.message_id for row in source.messages)
    if ids[:len(expected_ids)] != expected_ids:
        raise ConversationCompactError('主种子与transcript来源不一致', code='COMPACT_SOURCE_CHANGED')
    # 联合来源不能越过未列入覆盖的历史尾部；只压历史仍可保守截到原选源终点。
    joint_end = _joint_prefix_end(frozen, tool_source, selection) if ids == expected_ids else 0
    tail = AnthropicMessageAdapter().to_provider_messages([
        item for item in frozen.tool_ir_history if not is_applied_compact_summary(item)
    ])
    # 工具边界必须与真实主投影逐项相等，不能靠另一次Adapter的长度碰巧对齐。
    if joint_end and projection.messages[len(base):joint_end] != tail[:joint_end - len(base)]:
        joint_end = 0
    return TranscriptInputPrefix(tuple(deepcopy(projection.messages)), len(summary), rows, expected_ids, joint_end)


# LLM: 只允许工具来源位于最新动态事实之前的联合覆盖；不跨过保护事实，不把档案另拼到请求末尾。
# 函数用途: 将已证明身份的工具分区映射到主IR里的完整条目终点，无法证明时只压历史。
def _joint_prefix_end(frozen, source, selection):
    if source is None:
        return 0
    history_end = len(frozen.provider_history_messages) + len(AnthropicMessageAdapter().to_provider_messages([
        item for item in frozen.tool_ir_history if is_applied_compact_summary(item)
    ]))
    ir = [item for item in frozen.tool_ir_history if not is_applied_compact_summary(item)]
    protected = next((i for i, item in enumerate(ir) if isinstance(item, RuntimeFactsTurn)), len(ir))
    eligible = ir[:protected]
    if any(item not in eligible for item in source.source_ir_history):
        return 0
    indices = [i for i, item in enumerate(eligible) if item in source.source_ir_history]
    handoffs = _proved_handoff_positions(eligible, source, (frozen, selection.params))
    if source.source_records and not handoffs:
        return 0
    ends = indices + handoffs
    if not ends:
        return 0
    chosen = eligible[:max(ends) + 1]
    if any(not _joint_item_known(item, index, source, handoffs) for index, item in enumerate(chosen)):
        return 0
    return history_end + len(AnthropicMessageAdapter().to_provider_messages(chosen))


# LLM: 归档身份不从handoff正文猜；按同次参数的原记录与冻结工具上下文重放原构造器，逐字相等才证明对应关系。
# 函数用途: 排除其它记录或截断布局不能重放的交接，未匹配handoff不拉长覆盖终点。
def _proved_handoff_positions(eligible, source, binding):
    from ..conversation.tool_context_window import native_carried_tool_handoff

    frozen, params = binding
    if not source.source_records or params is None:
        return []
    records = list(getattr(params, 'archive_tool_calls', ()) or ())
    selected = list(source.source_records) + list(getattr(source, 'retained_records', ()))
    visible = [record for record in records if record in selected]
    if any(record not in visible for record in source.source_records):
        return []
    return [i for i, item in enumerate(eligible)
            if isinstance(item, CompactionSummary) and item.source == 'carried_tool_handoff'
            and item.text == native_carried_tool_handoff(
                list(frozen.tool_context), visible, max_chars=max(12000, len(item.text)))]


# LLM: 未选中的工具/未知IR是覆盖屏障；当前初始用户要求只作不可撤回的任务上下文，不授予新的覆盖身份。
# 函数用途: 要求工具区前缀闭包，只允许已选IR、精确交接及保留的当前opener。
def _joint_item_known(item, index, source, handoffs):
    from ..backends.tool_ir import UserTurn

    return (item in source.source_ir_history or index in handoffs
            or (index == 0 and isinstance(item, UserTurn) and not item.input_ids))
