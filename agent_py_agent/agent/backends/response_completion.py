# LLM: 三种 provider 协议共用未完成事实投影；只读结构字段，不推测正文、不调度恢复、不修改模型配置。
# 模块用途: 区分空响应、仅思考和未完成响应，保留原停止原因及已生成内容。
from __future__ import annotations

from typing import Any

_LENGTH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
_ERROR_REASONS = {
    "stream_eof": "MODEL_STREAM_INCOMPLETE",
    "invalid_tool_arguments": "MODEL_TOOL_ARGUMENTS_INVALID",
    "content_filter": "MODEL_RESPONSE_CONTENT_FILTERED",
}


# LLM: 仅按协议类型和非空载荷识别思考；不解析其含义、不视为公开回复，不改签名或密文。
# 函数用途: 避免把真实思考误判为空响应；供后端和工具循环共同判断是否已生成内容。
def has_reasoning_content(blocks: list[dict[str, Any]]) -> bool:
    for block in blocks:
        kind = block.get("type")
        if kind in {"thinking", "redacted_thinking"}:
            value = block.get("thinking" if kind == "thinking" else "data")
            if isinstance(value, str) and value.strip():
                return True
        elif kind == "responses_reasoning":
            from .responses_wire import reasoning_item

            if isinstance(block.get("model"), str) and reasoning_item(block.get("item")):
                return True
    return False


# LLM: 空字典表示没有不完整事实；长度优先于同轮半截参数，未知原因仍是错误而不是长度上限。
# 函数用途: 给 ModelResponse 填写统一终态，原 stop_reason 原样保留，不额外发起任何请求。
def incomplete_response_fields(stop_reason: str, *, incomplete_reason: str = "") -> dict[str, Any]:
    if stop_reason in _LENGTH_REASONS:
        status, reason, end = "unfinished", "MODEL_RESPONSE_TRUNCATED", "max-tokens"
    elif incomplete_reason or stop_reason == "content_filter":
        status = "error"
        reason = _ERROR_REASONS.get(incomplete_reason or stop_reason, "MODEL_RESPONSE_INCOMPLETE")
        end = "error"
    else:
        return {}
    return {"truncated": True, "stop_reason": stop_reason, "runtime_status": status,
            "runtime_reason": reason, "runtime_source": "model_provider", "turn_end_reason": end}


# LLM: 丢弃的工具不能进入后续 canonical IR；正文、完整思考和签名保持原值、原序，不制造结果配对。
# 函数用途: 返回可保留的 assistant 内容副本，避免未执行工具使整条回复无法归档。
def without_tool_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(block) for block in blocks if block.get("type") != "tool_use"]


# LLM: Chat/Messages 每次传入一份已观察工具序列；只读真实 name，不猜类型、不生成调用，调整时核对两种协议。
# 函数用途: 收集本次未完成响应中被丢弃工具的工具名，去重保序，供工具循环做有界恢复。
def truncated_tool_names(source: object) -> list[str]:
    names: list[str] = []
    considered = source if isinstance(source, (list, tuple)) else ()
    for item in considered:
        name = _observed_tool_name(item)
        if name and name not in names:
            names.append(name)
    return names


# LLM: 兼容三种真实来源：纯字符串名、已解析块、OpenAI 原始 tool_calls 条目；其它形态一律忽略。
# 函数用途: 从单个条目取出供应商给出的工具名，取不到就返回空串。
def _observed_tool_name(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    name = str(item.get("name") or "").strip()
    if name:
        return name
    function = item.get("function")
    return str(function.get("name") or "").strip() if isinstance(function, dict) else ""


# LLM: 响应诊断只裁剪错误展示，不得替换 canonical 响应或影响重试分类；调用方需遵守私有错误展示边界。
# 函数用途: 为供应商响应解析错误生成有界预览，不写文件或发请求。
def response_preview(obj: object, *, max_chars: int = 1000) -> str:
    text = str(obj)
    return text if len(text) <= max_chars else text[:max_chars] + "... [truncated]"
