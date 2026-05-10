# LLM: Tool-call context reducer keeps huge tool payloads out of the next live prompt.
# 模块用途: 当模型一次性生成很大的 write_file/append_file 参数时，只把摘要放回下一轮上下文，避免 prompt 爆炸。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


# LLM: AssistantToolRoundContextRequest bundles reducer limits with parsed tool calls.
# 类用途: 保存本轮模型回复、已解析工具调用和摘要预算，供 live prompt 压缩使用。
@dataclass(frozen=True)
class AssistantToolRoundContextRequest:
    response_text: str
    tool_calls: list[dict[str, Any]]
    max_inline_chars: int = 4000
    max_value_preview_chars: int = 240


# LLM: render_assistant_tool_round_context summarizes large tool-call payloads before the next model turn.
# 函数用途: 小回复原样保留；大回复只保留自然语言摘要、工具名、路径、字段大小和 hash。
def render_assistant_tool_round_context(request: AssistantToolRoundContextRequest) -> str:
    if not _should_reduce_tool_round(request):
        return request.response_text
    lines = [
        "[assistant tool-call response summarized]",
        f"- original_response_chars: {len(request.response_text)}",
        f"- tool_call_count: {len(request.tool_calls)}",
    ]
    lead = _non_tool_text_preview(request.response_text, request.max_value_preview_chars)
    if lead:
        lines.append(f"- non_tool_text_preview: {lead}")
    for index, payload in enumerate(request.tool_calls, start=1):
        lines.extend(_tool_call_summary_lines(index, payload, request.max_value_preview_chars))
    return "\n".join(lines)


# LLM: _should_reduce_tool_round detects responses that would bloat every later prompt.
# 函数用途: 只在回复本身过大或任意工具参数过大时压缩，避免影响普通短工具调用。
def _should_reduce_tool_round(request: AssistantToolRoundContextRequest) -> bool:
    if len(request.response_text) > request.max_inline_chars:
        return True
    return any(
        _value_size(value) > request.max_value_preview_chars * 4
        for payload in request.tool_calls
        for key, value in payload.items()
        if key != "tool"
    )


# LLM: _tool_call_summary_lines renders one parsed tool call without copying large parameter bodies.
# 函数用途: 把 path、content 等字段转成可恢复摘要；完整正文只应存在目标文件或调试详情里。
def _tool_call_summary_lines(index: int, payload: dict[str, Any], max_preview_chars: int) -> list[str]:
    tool = str(payload.get("tool") or "unknown")
    lines = [f"- tool_call_{index}: tool={tool}"]
    for key, value in payload.items():
        if key == "tool":
            continue
        lines.append(f"  - {key}: {_value_summary(value, max_preview_chars)}")
    return lines


# LLM: _value_summary gives model-useful metadata for strings, lists, dicts, and scalars.
# 函数用途: 对大字段输出长度、sha256 和短预览；对小字段保持可读值。
def _value_summary(value: Any, max_preview_chars: int) -> str:
    if isinstance(value, str):
        if len(value) <= max_preview_chars:
            return value
        return _large_text_summary(value, max_preview_chars)
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(text) <= max_preview_chars:
            return text
        return _large_text_summary(text, max_preview_chars)
    return repr(value)


# LLM: _large_text_summary keeps hashes stable so agents can compare without reloading bodies.
# 函数用途: 为大文本生成字节数、字符数、hash 和短预览，阻止正文进入 live prompt。
def _large_text_summary(text: str, max_preview_chars: int) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    preview = text[:max_preview_chars].replace("\n", "\\n")
    return (
        f"<large text omitted chars={len(text)} bytes={len(text.encode('utf-8'))} "
        f"sha256={digest} preview={preview}>"
    )


# LLM: _non_tool_text_preview keeps the assistant's planning sentence while removing TOOL_CALL blocks.
# 函数用途: 提取工具调用之外的少量说明文字，避免模型忘记上一轮自己为什么调用工具。
def _non_tool_text_preview(text: str, max_preview_chars: int) -> str:
    without_calls = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", " ", text, flags=re.DOTALL)
    compact = " ".join(without_calls.split())
    return compact[:max_preview_chars]


# LLM: _value_size estimates serialized parameter size without assuming every value is a string.
# 函数用途: 用统一预算判断嵌套 dict/list 或长字符串是否需要压缩。
def _value_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except TypeError:
        return len(repr(value))
