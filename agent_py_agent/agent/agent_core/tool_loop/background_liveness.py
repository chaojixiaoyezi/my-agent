from __future__ import annotations

# LLM: These sources have a durable conversation plus a scheduler/event path that can wake the task later.
# 模块用途: 给普通工具循环判断“后台派工后能否安全把当前 turn 交还用户”提供唯一来源名单。
WAKE_CAPABLE_SOURCES = frozenset({"background_main_agent", "chat", "gateway"})


def is_wake_capable_source(params: object) -> bool:
    return str(getattr(params, "source", "") or "").strip() in WAKE_CAPABLE_SOURCES


__all__ = ["WAKE_CAPABLE_SOURCES", "is_wake_capable_source"]
