# LLM: Rejection-context rendering stays separate from delivery repair contract extraction.
# 模块用途: 把被拒绝的阶段修复工具调用渲染成紧凑机器上下文，避免主 guard 文件继续膨胀。

from __future__ import annotations

import json


# LLM: render_delivery_repair_rejection_context returns machine-readable feedback for ignored required repairs.
# 函数用途: 生成包含 rejected_tool_calls 和 required_tool_calls 的结构化上下文，供下一轮模型直接修正工具选择。
def render_delivery_repair_rejection_context(
    payload: dict[str, object],
    calls: list[dict[str, object]],
    repairs: int,
    max_repairs: int,
) -> str:
    if not payload or repairs >= max_repairs:
        return ""
    rejection_payload = {
        **payload,
        "rejected_tool_calls": [_compact_call(call) for call in calls if isinstance(call, dict)],
        "rejection_code": "NON_PRODUCTIVE_DURING_REQUIRED_REPAIR",
    }
    return "\n".join(
        [
            "[tool-system delivery-required-repair-rejected]",
            json.dumps(rejection_payload, ensure_ascii=False, sort_keys=True),
            "上一轮工具调用没有推进 required_actions，系统未执行这些检查/空转调用。"
            "下一轮必须调用 required_tool_calls 中匹配的真实写入或构建工具。",
        ]
    )


# LLM: _compact_call keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _compact_call(call: dict[str, object]) -> dict[str, object]:
    record: dict[str, object] = {"tool": str(call.get("tool") or "").strip()}
    _copy_compact_field(record, call, "path", 240)
    _copy_compact_field(record, call, "file_path", 240)
    _copy_compact_field(record, call, "target_path", 240)
    _copy_compact_field(record, call, "command", 200)
    _copy_compact_field(record, call, "url", 240)
    _copy_compact_field(record, call, "artifact_ref", 240)
    return {key: value for key, value in record.items() if value}


# LLM: _copy_compact_field keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _copy_compact_field(record: dict[str, object], call: dict[str, object], key: str, limit: int) -> None:
    value = str(call.get(key) or "").strip()
    if not value:
        return
    output_key = "path" if key in {"file_path", "target_path"} else key
    record.setdefault(output_key, value[:limit])


__all__ = ["render_delivery_repair_rejection_context"]
