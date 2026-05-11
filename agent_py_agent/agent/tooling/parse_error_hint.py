# LLM: Parse-error repair hints stay isolated so registry execution remains a thin dispatcher.
# 模块用途: 生成工具调用解析失败后的模型重试提示，不回显坏工具正文。

from __future__ import annotations

from typing import Any

_PARSE_RETRY_HINT = (
    "请重新输出标准工具调用格式：[TOOL_CALL] 后跟一个 JSON 对象，再用 [/TOOL_CALL] 结束；"
    "不要混用未闭合的 XML 标签，也不要在 JSON 外追加正文。"
)


# LLM: parse_error_message gives the model a compact repair instruction without echoing raw tool bodies.
# 函数用途: 工具调用解析失败时返回固定重试格式提示，避免模型继续用同一种坏格式空转。
def parse_error_message(payload: dict[str, Any]) -> str:
    error = str(payload.get("error") or "工具调用解析失败")
    return f"{error}。{_PARSE_RETRY_HINT}"
