# LLM: 三种 provider 协议共用未完成事实投影；只读结构字段，不推测正文、不调度恢复、不修改模型配置。
# 模块用途: 区分输出上限、断流、坏工具参数和其它未完成响应，保留原停止原因与部分正文。
from __future__ import annotations

from typing import Any

_LENGTH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
_ERROR_REASONS = {
    "stream_eof": "MODEL_STREAM_INCOMPLETE",
    "invalid_tool_arguments": "MODEL_TOOL_ARGUMENTS_INVALID",
    "content_filter": "MODEL_RESPONSE_CONTENT_FILTERED",
}


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
