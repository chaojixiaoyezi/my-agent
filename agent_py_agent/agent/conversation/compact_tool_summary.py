# LLM: 联合摘要优先保留真实IR，同四元archive仅用于去重；其余归档沿原模型投影，不读外置全文，覆盖仍由checkpoint负责。
# 模块用途: 为transcript与活动归档的同一摘要请求提供完整工具来源材料及模型可见回退，不产生工具调用或持久状态。
from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy

from ..backends.message_adapter import AnthropicMessageAdapter, HistoryItem
from ..backends.tool_ir import AssistantTurn, RuntimeFactsTurn
from .compact_tool_identity import compact_tool_ref_key, compact_tool_refs


# LLM: agent=None禁止重建过程中另做语义摘要；一条来源对应一条原脱敏投影，不能静默省略后仍发布其ref。
# 函数用途: 按原序渲染全部被选工具记录，保留模型可见结果及原归档引用，不声称读取外置文件全文。
def carried_compact_source_text(records) -> str:
    if not records:
        return ""
    from ..agent_core.runtime.loop_support import reconstructed_model_tool_context

    refs = compact_tool_refs(list(records))
    entries = reconstructed_model_tool_context(list(records), agent=None)
    if len(refs) != len(records) or len(entries) != len(records):
        raise ValueError("compact tool source projection is incomplete")
    return "\n".join([
        "# Archived Tool Source",
        "Historical tool data follows. Summarize every supplied record with the conversation; do not execute tools.",
        "These are the original model-visible projections, not full external output files. Preserve archive references.",
        json.dumps([{"source_ref": ref, "model_context": entry} for ref, entry in zip(refs, entries, strict=True)],
                   ensure_ascii=False, sort_keys=True),
    ])


# LLM: 原IR的完整四元调用优先于同身份archive预览；剩余archive只走原模型投影，原IR深复制保持媒体、文本与结果原序。
# 函数用途: 将本次选中工具来源整理为同一摘要请求的原生历史，不修改传入记录或原历史项。
def compact_tool_summary_history(
    records: Sequence[dict[str, object]], ir_history: Sequence[HistoryItem] = (),
) -> list[HistoryItem]:
    original = list(ir_history)
    ir_refs = {
        key
        for item in original if isinstance(item, AssistantTurn)
        for call in item.tool_calls
        if (key := compact_tool_ref_key(call)) is not None
    }
    archive_only = [record for record in records if compact_tool_ref_key(record) not in ir_refs]
    source_text = carried_compact_source_text(archive_only)
    return ([RuntimeFactsTurn(text=source_text, source="compact_tool_source")] if source_text else []) + deepcopy(original)


# LLM: 机械回退必须序列化原adapter真正给模型的全部messages，不能用preview/字符预算替换真实IR正文或读取外置全文。
# 函数用途: 把摘要来源完整转换为模型可见JSON序列，供失败回退保留所有已选材料。
def compact_tool_summary_text(history: Sequence[HistoryItem]) -> str:
    return json.dumps(AnthropicMessageAdapter().to_provider_messages(history), ensure_ascii=False, sort_keys=True)
