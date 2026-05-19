# LLM: read_file execution is isolated so filesystem tool declarations stay small.
# 模块用途: 执行工作区内文本文件读取、行号分页、结构化摘要和 artifact 读取提示。

from __future__ import annotations

import json
from typing import Any

from ._filesystem_helpers import _bundled_filesystem_param, _int_param, _required_path
from .file_write_session_inspection import open_file_write_sessions
from .filesystem_artifact_guard import allowed_tools_hint_param, tool_output_artifact_read_hint
from .filesystem_structured_read import structured_read_summary
from .models import ToolExecutionResult


# LLM: execute_read_file carries the full read_file behavior for FileSystemTool subclasses.
# 函数用途: 解析 read_file 参数、校验 artifact 读取边界、读取文本并按行号/字符预算返回结果。
def execute_read_file(tool, params: dict[str, Any], max_chars: int) -> ToolExecutionResult:
    try:
        raw_path = _required_path(_bundled_filesystem_param(params, "path"))
        target = tool.resolve_path(raw_path)
    except ValueError as exc:
        return ToolExecutionResult("read_file", False, str(exc))
    artifact_hint = tool_output_artifact_read_hint(
        target,
        tool.workspace_roots,
        allowed_tools=allowed_tools_hint_param(params),
    )
    if artifact_hint:
        return ToolExecutionResult("read_file", False, artifact_hint)
    if not target.exists():
        pending_session = _pending_write_session_result(tool, target)
        if pending_session:
            return pending_session
        return ToolExecutionResult("read_file", False, f"文件不存在: {tool.display_path(target)}")
    if not target.is_file():
        return ToolExecutionResult("read_file", False, f"目标不是文件: {tool.display_path(target)}")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolExecutionResult("read_file", False, "文件不是有效 UTF-8 文本，无法读取。")
    summary = structured_read_summary(target, content, params)
    if summary:
        return ToolExecutionResult("read_file", True, summary)
    return _numbered_text_result(content, params, max_chars)


# LLM: _pending_write_session_result exposes staged write state when the final target is not materialized.
# 函数用途: read_file 读最终文件但文件还没 finish 时，返回结构化 file_write_session 续写合同。
def _pending_write_session_result(tool, target) -> ToolExecutionResult | None:
    for session in open_file_write_sessions(tool.workspace_root, limit=100):
        target_path = session.get("target_path") if isinstance(session.get("target_path"), dict) else {}
        resolved = str(target_path.get("resolved") or "")
        if resolved != str(target):
            continue
        envelope = {
            "code": "TARGET_PENDING_FILE_WRITE_SESSION",
            "target_path": target_path,
            "recommended_session_id": str(session.get("session_id") or ""),
            "manifest_path": str(session.get("manifest_path") or ""),
            "preview_path": str(session.get("preview_path") or ""),
            "preview_materialized": bool(session.get("preview_materialized")),
            "received_chunks": list(session.get("received_chunks") or []),
            "next_chunk_index": int(session.get("next_chunk_index") or 0),
            "recommended_tool_call": session.get("continue_tool_call") or {},
            "finish_tool_call": session.get("finish_tool_call") or {},
        }
        return ToolExecutionResult(
            "read_file",
            False,
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            result_envelope=envelope,
            error_code="TARGET_PENDING_FILE_WRITE_SESSION",
            recommended_action="continue_pending_file_write_session",
            recovery_hint="continue file_write_session by recommended_session_id and finish before reading final target",
        )
    return None


# LLM: _numbered_text_result turns raw text into bounded line-aware output.
# 函数用途: 处理 start_line/end_line、空文件、越界和截断提示。
def _numbered_text_result(content: str, params: dict[str, Any], max_chars: int) -> ToolExecutionResult:
    lines = content.splitlines()
    raw_end_line = _bundled_filesystem_param(params, "end_line")
    try:
        start_line = _int_param(
            _bundled_filesystem_param(params, "start_line"),
            name="start_line",
            default=1,
            min_value=1,
        )
        end_line = _int_param(
            raw_end_line,
            name="end_line",
            default=max(len(lines), 1),
            min_value=1,
        )
    except ValueError as exc:
        return ToolExecutionResult("read_file", False, str(exc))
    error = _line_range_error(lines, start_line, end_line, raw_end_line)
    if error:
        return ToolExecutionResult("read_file", False, error)
    if not lines:
        return ToolExecutionResult("read_file", True, "(空文件)")
    result = _render_numbered_read_lines(
        lines=lines,
        start_line=start_line,
        end_line=end_line,
        max_chars=max_chars,
    )
    return ToolExecutionResult("read_file", True, result or "(空文件)")


# LLM: _line_range_error returns actionable range errors without reading more text.
# 函数用途: 判断空文件、超过文件末尾和 end_line 小于 start_line 的错误提示。
def _line_range_error(lines: list[str], start_line: int, end_line: int, raw_end_line: object) -> str:
    total_lines = len(lines)
    if not lines and start_line > 1:
        return "start_line 超出文件末尾：total_lines=0，文件为空。"
    if not lines:
        return ""
    if start_line > total_lines and raw_end_line is None:
        return _past_eof_line_message(start_line, total_lines)
    if end_line < start_line:
        return "end_line 不能小于 start_line"
    if start_line > total_lines:
        return _past_eof_line_message(start_line, total_lines)
    return ""


# LLM: _render_numbered_read_lines keeps long file reads line-aware so agents can continue by line.
# 函数用途: 按行号输出文件切片；达到字符上限时给出 total_lines 和 next_start_line，避免模型猜尾行。
def _render_numbered_read_lines(
    *,
    lines: list[str],
    start_line: int,
    end_line: int,
    max_chars: int,
) -> str:
    rendered: list[str] = []
    used_chars = 0
    for line_number, line in enumerate(lines[start_line - 1 : end_line], start=start_line):
        item = f"{line_number}: {line}"
        separator = 1 if rendered else 0
        if rendered and used_chars + separator + len(item) > max_chars:
            rendered.append(_truncated_read_footer(len(lines), line_number, max_chars))
            return "\n".join(rendered)
        if not rendered and len(item) > max_chars:
            return "\n".join([
                item[:max_chars],
                _truncated_read_footer(len(lines), min(line_number + 1, len(lines)), max_chars),
            ])
        rendered.append(item)
        used_chars += separator + len(item)
    return "\n".join(rendered)


# LLM: _truncated_read_footer gives the model a deterministic continuation cursor.
# 函数用途: 生成 read_file 截断提示，包含总行数、下一次建议 start_line 和当前字符预算。
def _truncated_read_footer(total_lines: int, next_start_line: int, max_chars: int) -> str:
    return (
        f"... 已截断; total_lines={total_lines}; "
        f"next_start_line={next_start_line}; limit_chars={max_chars}"
    )


# LLM: _past_eof_line_message turns empty tail reads into actionable line-range guidance.
# 函数用途: 当模型读到超过文件末尾的行号时，明确告诉它总行数和建议的尾部读取范围。
def _past_eof_line_message(start_line: int, total_lines: int) -> str:
    suggested_start = max(1, total_lines - 80 + 1)
    return (
        f"start_line 超出文件末尾：start_line={start_line}, total_lines={total_lines}。"
        f"请改用 start_line={suggested_start}, end_line={total_lines} 读取文件尾部，"
        "或用 search_text 搜索目标标签。"
    )
