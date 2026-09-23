# LLM: Compact覆盖只使用原ToolCall四元身份；旧归档缺字段保持未知，不从当前runner或scoped_call_id补身份。
# 模块用途: 在摘要提交和恢复投影之间共用精确工具引用，防止不同回合复用call_id时误隐藏。
from __future__ import annotations

from collections.abc import Mapping

from .compact_guard import ConversationCompactError

TOOL_REF_FIELDS = ("run_id", "attempt_id", "turn_id", "call_id")


# LLM: 值只能来自原调用或原归档；返回None表示旧身份不完整，调用方不得将None视为通配。
# 函数用途: 读取四元调用身份，既支持原ToolCall，也支持真实归档记录。
def compact_tool_ref(value: object) -> dict[str, str] | None:
    fields = {
        key: value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        for key in TOOL_REF_FIELDS
    }
    if any(not isinstance(item, str) or not item.strip() for item in fields.values()):
        return None
    return {key: item.strip() for key, item in fields.items()}


# LLM: 缺维度不能折叠为同一键；本键只用于实际应用摘要后的精确覆盖匹配。
# 函数用途: 将完整引用转为可比较键，未知旧来源返回空。
def compact_tool_ref_key(value: object) -> tuple[str, ...] | None:
    ref = compact_tool_ref(value)
    return tuple(ref[key] for key in TOOL_REF_FIELDS) if ref is not None else None


# LLM: 来源选择必须先按完整记录/IR完成，不能用裸call_id在不同模型轮之间选记录；未知身份拒绝提交。
# 函数用途: 按执行顺序固定完整四元引用，同名调用属于不同轮时全部保留。
def compact_tool_refs(values: list[object]) -> tuple[dict[str, str], ...]:
    selected: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        ref = compact_tool_ref(value)
        if ref is None:
            raise ConversationCompactError("压缩工具来源身份不完整", code="COMPACT_TOOL_COVERAGE_UNKNOWN")
        key = tuple(ref[field] for field in TOOL_REF_FIELDS)
        if key not in seen:
            selected.append(ref)
            seen.add(key)
    return tuple(selected)
