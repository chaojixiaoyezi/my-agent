# LLM: Subagent session compact payload extraction is shared by finalize and auto-continuation.
# 模块用途: 从 AgentRunResult 提取子代理本地 compact 需要的小字段，避免主 memory apply 参与。

from __future__ import annotations

"""Helpers for converting AgentRunResult compact hints into subagent-local payloads."""


# LLM: subagent_session_compact_payload_from_result keeps save=False runner compact facts bounded.
# 函数用途: 只提取 compact 状态、比例、建议命令和 token 预算，不复制 prompt/response 正文。
def subagent_session_compact_payload_from_result(result: object) -> dict[str, object]:
    if not bool(getattr(result, "memory_compact_suggested", False)):
        return {}
    return {
        "suggested": True,
        "status": str(getattr(result, "memory_compact_status", "") or ""),
        "auto_status": str(getattr(result, "memory_compact_auto_status", "") or ""),
        "ratio": float(getattr(result, "memory_compact_ratio", 0.0) or 0.0),
        "message": str(getattr(result, "memory_compact_message", "") or ""),
        "commands": list(getattr(result, "memory_compact_commands", []) or []),
        "token_budget": {
            "current_tokens": int(getattr(result, "cumulative_token_estimate", 0) or 0),
            "turn_tokens": int(getattr(result, "turn_token_estimate", 0) or 0),
            "prompt_tokens": int(getattr(result, "prompt_token_estimate", 0) or 0),
        },
        "continue_packet": dict(getattr(result, "memory_compact_auto_continue_packet", None) or {}),
    }


__all__ = ["subagent_session_compact_payload_from_result"]
