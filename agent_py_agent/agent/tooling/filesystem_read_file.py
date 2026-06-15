
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..path_recovery_hints import suggest_workspace_typo_target
from ._filesystem_helpers import (
    _int_param,
    _internal_agent_status_ref,
    _required_path,
)
from .filesystem_artifact_guard import tool_output_artifact_content, tool_output_artifact_typo_hint
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .filesystem_structured_read import structured_read_summary
from .models import ToolExecutionResult


@dataclass(frozen=True)
class ReadFileRequest:
    tool: Any
    params: dict[str, Any]
    max_chars: int
    raw_path: str
    target: Path


@dataclass(frozen=True)
class CharWindowFileRequest:
    tool: Any
    target: Path
    params: dict[str, Any]
    default_max_chars: int


@dataclass(frozen=True)
class CharWindowView:
    offset: int
    window: str
    total_chars: int
    requested_limit: int
    default_max_chars: int


@dataclass(frozen=True)
class NumberedReadLinesRequest:
    lines: list[str]
    start_line: int
    end_line: int
    max_chars: int
    continuation_max_chars: int


@dataclass(frozen=True)
class TruncatedReadFooterRequest:
    total_lines: int
    next_start_line: int
    max_chars: int
    continuation_max_chars: int
    next_offset: int | None = None


def execute_read_file(tool, params: dict[str, Any], max_chars: int) -> ToolExecutionResult:
    try:
        raw_path = _required_path(params.get("path"))
        target = tool.resolve_path(raw_path)
    except ValueError as exc:
        return ToolExecutionResult("read_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
    return _execute_read_file_request(ReadFileRequest(
        tool=tool,
        params=params,
        max_chars=max_chars,
        raw_path=raw_path,
        target=target,
    ))


def _execute_read_file_request(request: ReadFileRequest) -> ToolExecutionResult:
    target = request.target
    if not target.exists():
        typo_hint = _missing_tool_artifact_typo_hint(request.tool, request.raw_path)
        if typo_hint:
            return ToolExecutionResult("read_file", False, typo_hint, error_code="PATH_NOT_FOUND")
        return missing_path_result(MissingPathRequest(
            tool_name="read_file",
            raw_path=request.raw_path,
            target=target,
            workspace_roots=request.tool.workspace_roots,
            display_path=request.tool.display_path(target),
            expected_kind="file",
            retry_tool="read_file",
        ))
    if not target.is_file():
        return _not_file_result(request.tool, target)
    internal_ref = _internal_agent_status_ref(target)
    if internal_ref:
        return ToolExecutionResult(
            "read_file",
            False,
            json.dumps(internal_ref, ensure_ascii=False, indent=2),
            error_code="WRONG_STATUS_SURFACE",
        )
    artifact_content = tool_output_artifact_content(target, request.tool.workspace_roots)
    if artifact_content:
        return _numbered_text_result(artifact_content, request.params, request.max_chars)
    if _has_char_window_params(request.params):
        return _char_window_file_result(CharWindowFileRequest(
            tool=request.tool,
            target=target,
            params=request.params,
            default_max_chars=request.max_chars,
        ))
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolExecutionResult("read_file", False, "文件不是有效 UTF-8 文本，无法读取。", error_code="TOOL_EXECUTION_FAILED")
    summary = structured_read_summary(target, content, request.params)
    if summary:
        return ToolExecutionResult("read_file", True, summary)
    return _numbered_text_result(content, request.params, request.max_chars)


def _not_file_result(tool, target: Path) -> ToolExecutionResult:
    if target.is_dir():
        display_path = tool.display_path(target)
        payload = {
            "ok": False,
            "error": "PATH_IS_DIRECTORY",
            "path": display_path,
            "message": "目标是目录，不是文件；请先用 list_files 查看目录，再读取具体文件。",
            "suggested_tool_call": {"tool": "list_files", "path": display_path, "max_depth": 1},
        }
        return ToolExecutionResult(
            "read_file",
            False,
            json.dumps(payload, ensure_ascii=False, indent=2),
            error_code="PATH_IS_DIRECTORY",
        )
    return ToolExecutionResult("read_file", False, f"目标不是文件: {tool.display_path(target)}", error_code="PATH_INVALID")


def _numbered_text_result(content: str, params: dict[str, Any], max_chars: int) -> ToolExecutionResult:
    if _has_char_window_params(params):
        return _char_window_result(content, params, max_chars)
    lines = content.splitlines()
    raw_end_line = params.get("end_line")
    try:
        start_line = _int_param(
            params.get("start_line"),
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
        line_max_chars = _line_max_chars(params, max_chars)
    except ValueError as exc:
        return ToolExecutionResult("read_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
    error = _line_range_error(lines, start_line, end_line, raw_end_line)
    if error:
        return ToolExecutionResult("read_file", False, error, error_code="TOOL_INVALID_ARGUMENTS")
    if not lines:
        return ToolExecutionResult("read_file", True, "(空文件)")
    result, read_window = _render_numbered_read_lines(NumberedReadLinesRequest(
        lines=lines,
        start_line=start_line,
        end_line=end_line,
        max_chars=line_max_chars,
        continuation_max_chars=max_chars,
    ))
    envelope = {"read_window": read_window} if read_window else {}
    return ToolExecutionResult("read_file", True, result or "(空文件)", result_envelope=envelope)


def _has_char_window_params(params: dict[str, Any]) -> bool:
    if params.get("offset") is not None:
        return True
    if params.get("start_char") is not None:
        return True
    if params.get("start_line") is not None:
        return False
    if params.get("end_line") is not None:
        return False
    return params.get("max_chars") is not None


def _line_max_chars(params: dict[str, Any], default_max_chars: int) -> int:
    requested = _int_param(
        params.get("max_chars"),
        name="max_chars",
        default=default_max_chars,
        min_value=1,
    )
    return min(requested, default_max_chars)


def _char_window_result(content: str, params: dict[str, Any], default_max_chars: int) -> ToolExecutionResult:
    try:
        offset = _int_param(
            params.get("offset")
            if params.get("offset") is not None
            else params.get("start_char"),
            name="offset",
            default=0,
            min_value=0,
        )
        limit = _int_param(
            params.get("max_chars"),
            name="max_chars",
            default=default_max_chars,
            min_value=1,
        )
    except ValueError as exc:
        return ToolExecutionResult("read_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
    if offset >= len(content):
        return _offset_out_of_range_result(offset, len(content))
    window = content[offset : offset + min(limit, default_max_chars)]
    next_offset = offset + len(window)
    header = f"[char-window offset={offset} chars={len(window)} total_chars={len(content)}]"
    if next_offset < len(content):
        capped_limit = min(limit, default_max_chars)
        continuation_max_chars = _continuation_max_chars(capped_limit, default_max_chars)
        footer = (
            "PARTIAL view only; 这不是完整文件。"
            f" total_chars={len(content)}; next_offset={next_offset}; limit_chars={capped_limit};"
            f" recommended_next_max_chars={continuation_max_chars};"
            f" next_call=read_file(offset={next_offset}, max_chars={continuation_max_chars})。"
            " 最终报告前先把关键事实和 source offset 写入 task_progress 或 work 表。"
        )
        return ToolExecutionResult(
            "read_file",
            True,
            f"{header}\n{window}\n{footer}",
            result_envelope={"read_window": _char_read_window(offset, len(window), len(content))},
        )
    return ToolExecutionResult(
        "read_file",
        True,
        f"{header}\n{window}",
        result_envelope={"read_window": _char_read_window(offset, len(window), len(content))},
    )


def _char_window_file_result(request: CharWindowFileRequest) -> ToolExecutionResult:
    try:
        offset, limit = _char_window_values(request.params, request.default_max_chars)
        total_chars = _cached_total_chars(request.tool, request.target)
        if offset >= total_chars:
            return _offset_out_of_range_result(offset, total_chars)
        window = _read_char_window(
            request.target,
            offset=offset,
            limit=min(limit, request.default_max_chars),
        )
    except ValueError as exc:
        return ToolExecutionResult("read_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
    except UnicodeDecodeError:
        return ToolExecutionResult("read_file", False, "文件不是有效 UTF-8 文本，无法读取。", error_code="TOOL_EXECUTION_FAILED")
    except OSError as exc:
        return ToolExecutionResult("read_file", False, f"读取文件失败: {exc}", error_code="TOOL_EXECUTION_FAILED")
    return _char_window_view_result(CharWindowView(
        offset=offset,
        window=window,
        total_chars=total_chars,
        requested_limit=limit,
        default_max_chars=request.default_max_chars,
    ))


def _char_window_values(params: dict[str, Any], default_max_chars: int) -> tuple[int, int]:
    offset = _int_param(
        params.get("offset")
        if params.get("offset") is not None
        else params.get("start_char"),
        name="offset",
        default=0,
        min_value=0,
    )
    limit = _int_param(
        params.get("max_chars"),
        name="max_chars",
        default=default_max_chars,
        min_value=1,
    )
    return offset, limit


def _char_window_view_result(view: CharWindowView) -> ToolExecutionResult:
    next_offset = view.offset + len(view.window)
    header = f"[char-window offset={view.offset} chars={len(view.window)} total_chars={view.total_chars}]"
    if next_offset < view.total_chars:
        capped_limit = min(view.requested_limit, view.default_max_chars)
        continuation_max_chars = _continuation_max_chars(capped_limit, view.default_max_chars)
        footer = (
            "PARTIAL view only; 这不是完整文件。"
            f" total_chars={view.total_chars}; next_offset={next_offset}; limit_chars={capped_limit};"
            f" recommended_next_max_chars={continuation_max_chars};"
            f" next_call=read_file(offset={next_offset}, max_chars={continuation_max_chars})。"
            " 最终报告前先把关键事实和 source offset 写入 task_progress 或 work 表。"
        )
        return ToolExecutionResult(
            "read_file",
            True,
            f"{header}\n{view.window}\n{footer}",
            result_envelope={"read_window": _char_read_window(view.offset, len(view.window), view.total_chars)},
        )
    return ToolExecutionResult(
        "read_file",
        True,
        f"{header}\n{view.window}",
        result_envelope={"read_window": _char_read_window(view.offset, len(view.window), view.total_chars)},
    )


def _offset_out_of_range_result(offset: int, total_chars: int) -> ToolExecutionResult:
    return ToolExecutionResult(
        "read_file",
        False,
        f"offset 超出文件末尾：offset={offset}, total_chars={total_chars}。请改用更小的 offset。",
        error_code="OFFSET_OUT_OF_RANGE",
    )


def _cached_total_chars(tool, target) -> int:
    stat = target.stat()
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    cache = getattr(tool, "_read_file_char_count_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        tool._read_file_char_count_cache = cache
    cached = cache.get(key)
    if isinstance(cached, int):
        return cached
    total = _count_chars_streaming(target)
    cache.clear()
    cache[key] = total
    return total


def _count_chars_streaming(target, *, chunk_size: int = 256 * 1024) -> int:
    with target.open("r", encoding="utf-8") as handle:
        return sum(len(chunk) for chunk in iter(lambda: handle.read(chunk_size), ""))


def _read_char_window(target, *, offset: int, limit: int, chunk_size: int = 256 * 1024) -> str:
    parts: list[str] = []
    remaining_skip = offset
    remaining_read = limit
    with target.open("r", encoding="utf-8") as handle:
        if not _skip_chars(handle, remaining_skip, chunk_size):
            return ""
        while remaining_read > 0 and (chunk := handle.read(min(remaining_read, chunk_size))):
            parts.append(chunk)
            remaining_read -= len(chunk)
    return "".join(parts)


def _skip_chars(handle, remaining_skip: int, chunk_size: int) -> bool:
    while remaining_skip > 0:
        skipped = handle.read(min(remaining_skip, chunk_size))
        if not skipped:
            return False
        remaining_skip -= len(skipped)
    return True


def _missing_tool_artifact_typo_hint(tool, raw_path: str) -> str:
    suggested = suggest_workspace_typo_target(raw_path, tool.workspace_roots)
    if not suggested:
        return ""
    return tool_output_artifact_typo_hint(raw_path, tool.workspace_root, suggested)


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


def _render_numbered_read_lines(request: NumberedReadLinesRequest) -> tuple[str, dict[str, int | bool | str]]:
    rendered: list[str] = []
    used_chars = 0
    last_line = request.start_line - 1
    for line_number, line in enumerate(
        request.lines[request.start_line - 1 : request.end_line],
        start=request.start_line,
    ):
        item = f"{line_number}: {line}"
        separator = 1 if rendered else 0
        if rendered and used_chars + separator + len(item) > request.max_chars:
            rendered.append(_truncated_read_footer(TruncatedReadFooterRequest(
                total_lines=len(request.lines),
                next_start_line=line_number,
                max_chars=request.max_chars,
                continuation_max_chars=request.continuation_max_chars,
            )))
            return "\n".join(rendered), _line_read_window(request.start_line, last_line, len(request.lines))
        if not rendered and len(item) > request.max_chars:
            return (
                "\n".join([
                    item[:request.max_chars],
                    _truncated_read_footer(TruncatedReadFooterRequest(
                        total_lines=len(request.lines),
                        next_start_line=min(line_number + 1, len(request.lines)),
                        max_chars=request.max_chars,
                        continuation_max_chars=request.continuation_max_chars,
                        next_offset=request.max_chars,
                    )),
                ]),
                _line_read_window(request.start_line, line_number, len(request.lines)),
            )
        rendered.append(item)
        used_chars += separator + len(item)
        last_line = line_number
    return "\n".join(rendered), _line_read_window(request.start_line, last_line, len(request.lines))


def _char_read_window(offset: int, chars: int, total_chars: int) -> dict[str, int | bool | str]:
    next_offset = offset + chars
    return {
        "kind": "char_window",
        "offset": offset,
        "chars": chars,
        "next_offset": next_offset,
        "total_chars": total_chars,
        "complete": bool(total_chars and next_offset >= total_chars),
    }


def _line_read_window(start_line: int, end_line: int, total_lines: int) -> dict[str, int | bool | str]:
    end_line = max(0, end_line)
    return {
        "kind": "line_window",
        "start_line": start_line,
        "end_line": end_line,
        "next_start_line": end_line + 1 if end_line < total_lines else 0,
        "total_lines": total_lines,
        "complete": bool(total_lines and start_line <= 1 and end_line >= total_lines),
    }


def _truncated_read_footer(request: TruncatedReadFooterRequest) -> str:
    next_max_chars = _continuation_max_chars(request.max_chars, request.continuation_max_chars)
    footer = (
        "... 已截断；PARTIAL view only; 这不是完整文件。"
        f" total_lines={request.total_lines}; next_start_line={request.next_start_line};"
        f" limit_chars={request.max_chars};"
        f" recommended_next_max_chars={next_max_chars};"
        f" next_call=read_file(start_line={request.next_start_line}, max_chars={next_max_chars})。"
        " 最终报告前先把关键事实和 source 行号写入 task_progress 或 work 表。"
    )
    if request.next_offset is not None:
        footer += f"; next_offset={request.next_offset}"
    return footer


def _continuation_max_chars(current_limit: int, configured_max_chars: int) -> int:
    return max(1, max(int(current_limit or 0), int(configured_max_chars or 0)))


def _past_eof_line_message(start_line: int, total_lines: int) -> str:
    suggested_start = max(1, total_lines - 80 + 1)
    return (
        f"start_line 超出文件末尾：start_line={start_line}, total_lines={total_lines}。"
        f"请改用 start_line={suggested_start}, end_line={total_lines} 读取文件尾部，"
        "或用 search_text 搜索目标标签。"
    )
