# LLM: Parse-error repair hints stay isolated so registry execution remains a thin dispatcher.
# 模块用途: 生成工具调用解析失败后的模型重试提示，不回显坏工具正文。

from __future__ import annotations

from typing import Any

from .content_transport_policy import (
    RECOMMENDED_WRITE_CHUNK_CHARS,
    RECOVERY_WRITE_CHUNK_CHARS,
)

_PARSE_RETRY_HINT = (
    "请重新输出标准工具调用格式：[TOOL_CALL] 后跟一个 JSON 对象，再用 [/TOOL_CALL] 结束；"
    "不要混用未闭合的 XML 标签，也不要在 JSON 外追加正文。"
)
_TRUNCATED_PAYLOAD_HINT = (
    "如果上一轮工具参数太长导致截断，请缩短 goal/plan/acceptance_checks，"
    "只保留关键路径、必需文件名和硬约束；长说明交给后续 runner 自己展开。"
)
_TRUNCATED_WRITE_HINT = (
    "如果上一轮是 write_file/append_file 且 content 太长，不要重复输出完整 content；"
    "优先用 write_file 写短骨架，再用 append_file 分块追加内容；"
    f"正常分块时单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符。"
    "如果已经连续解析失败，下一轮只能输出 1 个 write_file/append_file 工具调用，"
    f"content 降到不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符，闭合 [/TOOL_CALL] 后再继续下一块。"
)


# LLM: parse_error_message gives the model a compact repair instruction without echoing raw tool bodies.
# 函数用途: 工具调用解析失败时返回固定重试格式提示，避免模型继续用同一种坏格式空转。
def parse_error_message(payload: dict[str, Any]) -> str:
    error = str(payload.get("error") or "工具调用解析失败")
    hint = f"{error}。{_PARSE_RETRY_HINT}"
    if "缺少结束标记" in error:
        hint = f"{hint}{_TRUNCATED_PAYLOAD_HINT}"
    raw = str(payload.get("raw") or "")
    is_write_payload = '"write_file"' in raw or '"append_file"' in raw
    if "缺少结束标记" in error and is_write_payload and '"content"' in raw:
        hint = f"{hint}{_TRUNCATED_WRITE_HINT}"
    return hint
