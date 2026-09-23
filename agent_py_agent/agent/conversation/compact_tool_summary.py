# LLM: 联合摘要沿原归档逐条模型投影，不读外置全文或套有界handoff；所有选中记录都交原分段器，覆盖仍由checkpoint负责。
# 模块用途: 为transcript与活动归档的同一摘要请求提供完整工具来源材料，不产生工具调用或持久状态。
from __future__ import annotations

import json

from .compact_tool_identity import compact_tool_refs


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


# LLM: 复用原IR适配器包装历史数据，source仅用于内部来源标记；不是新的事实权威或真实工具往返。
# 函数用途: 将完整工具来源追加到摘要请求的历史段，稳定系统和工具缓存面保持原格式。
def carried_compact_source_messages(text: str) -> list[dict[str, object]]:
    from ..backends.message_adapter import AnthropicMessageAdapter
    from ..backends.tool_ir import RuntimeFactsTurn

    return AnthropicMessageAdapter().to_provider_messages([
        RuntimeFactsTurn(text=text, source="compact_tool_source"),
    ])
