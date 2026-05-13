# LLM: Registry execution params helpers keep authorization hints out of the main dispatcher.
# 模块用途: 准备工具执行参数，并注入只用于错误提示的上下文。

from __future__ import annotations

from typing import Any


# LLM: tool_params_for_execution prepares execution params and injects read-only hint context when useful.
# 函数用途: 去掉 payload 的 tool 字段，并给 read_file 注入 allowed_tools 提示上下文；不改变实际授权。
def tool_params_for_execution(
    normalized_payload: dict[str, Any],
    tool_name: str,
    allowed_tools: list[str] | None,
) -> dict[str, Any]:
    params = {key: value for key, value in normalized_payload.items() if key != "tool"}
    if tool_name == "read_file" and allowed_tools is not None:
        params["__allowed_tools"] = list(allowed_tools)
    return params
