
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
    "如果上一轮是 write_file 且 content 太长，不要重复输出完整 content；"
    "优先用独立成行的 [WRITE_FILE_RAW path=\"...\"]...[/WRITE_FILE_RAW] 原文块或 write_file.data_base64 提交完整产物；"
    "不要把 WRITE_FILE_RAW 当 JSON tool 名；"
    f"正常分块时单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符。"
    "如果已经连续解析失败，下一轮只能输出 1 个 write_file 工具调用，"
    f"content 降到不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符，闭合 [/TOOL_CALL] 后再继续下一块。"
)


def parse_error_message(payload: dict[str, Any]) -> str:
    error = str(payload.get("error") or "工具调用解析失败")
    error_code = str(payload.get("error_code") or "").strip()
    hint = f"{error}。{_PARSE_RETRY_HINT}"
    if error_code == "TOOL_CALL_UNCLOSED":
        hint = f"{hint}{_TRUNCATED_PAYLOAD_HINT}"
    raw = str(payload.get("raw") or "")
    is_write_payload = '"write_file"' in raw
    if error_code == "TOOL_CALL_UNCLOSED" and is_write_payload and '"content"' in raw:
        hint = f"{hint}{_TRUNCATED_WRITE_HINT}"
    return hint
