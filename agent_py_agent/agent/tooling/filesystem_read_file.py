# LLM: read_file execution is isolated so filesystem tool declarations stay small.
# 模块用途: 执行工作区内文本文件读取、行号分页、结构化摘要和 artifact 读取提示。

from __future__ import annotations

from typing import Any

from ._filesystem_helpers import _bundled_filesystem_param, _int_param, _required_path
from .filesystem_artifact_guard import allowed_tools_hint_param, tool_output_artifact_read_hint
from .filesystem_structured_read import structured_read_summary
from .models import ToolExecutionResult

_LARGE_FILE_DIRECT_READ_BYTES = 2 * 1024 * 1024
_LOG_SEARCH_QUERIES = "ERROR,WARN,trace,timeout,exception,failed"
_DEFAULT_SEARCH_QUERIES = "TODO,FIXME,error,warning,trace,failed"


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
        return ToolExecutionResult("read_file", False, f"文件不存在: {tool.display_path(target)}")
    if not target.is_file():
        return ToolExecutionResult("read_file", False, f"目标不是文件: {tool.display_path(target)}")
    large_file_summary = _large_file_read_summary(tool, target, params)
    if large_file_summary:
        return ToolExecutionResult("read_file", True, large_file_summary)
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolExecutionResult("read_file", False, "文件不是有效 UTF-8 文本，无法读取。")
    summary = structured_read_summary(target, content, params)
    if summary:
        return ToolExecutionResult("read_file", True, summary)
    return _numbered_text_result(content, params, max_chars)


# LLM: _large_file_read_summary blocks wasteful direct reads and returns a structured next-step policy.
# 函数用途: 对未指定行号的大文件返回搜索/行号读取建议，避免把无效开头塞进模型上下文。
def _large_file_read_summary(tool, target, params: dict[str, Any]) -> str:
    if _has_explicit_line_range(params):
        return ""
    try:
        size_bytes = target.stat().st_size
    except OSError:
        return ""
    if size_bytes <= _LARGE_FILE_DIRECT_READ_BYTES:
        return ""
    return "\n".join(
        [
            "large_file_summary=true",
            f"path={tool.display_path(target)}",
            f"size_bytes={size_bytes}",
            f"direct_read_limit_bytes={_LARGE_FILE_DIRECT_READ_BYTES}",
            "read_policy=large_file_use_search_or_line_range",
            "suggested_next_tools=search_text,read_file",
            f"suggested_search_queries={_suggested_large_file_queries(target)}",
            "search_text_params=path:<this_file>, query:<keyword>, context:2, limit:20",
            "read_file_params=path:<this_file>, start_line:<line>, end_line:<line>",
        ]
    )


# LLM: _has_explicit_line_range keeps intentional paged reads from being intercepted by large-file policy.
# 函数用途: 判断调用方是否明确给了 start_line/end_line；显式行号读取仍按普通 read_file 执行。
def _has_explicit_line_range(params: dict[str, Any]) -> bool:
    return (
        _bundled_filesystem_param(params, "start_line") is not None
        or _bundled_filesystem_param(params, "end_line") is not None
    )


# LLM: _suggested_large_file_queries maps file shape to deterministic search seeds.
# 函数用途: 按文件扩展名给出结构化搜索关键词，不从用户自然语言里推断执行规则。
def _suggested_large_file_queries(target) -> str:
    if target.suffix.lower() in {".log", ".out", ".err"}:
        return _LOG_SEARCH_QUERIES
    return _DEFAULT_SEARCH_QUERIES


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
