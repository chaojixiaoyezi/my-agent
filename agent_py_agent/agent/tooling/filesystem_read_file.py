# LLM: 普通文件的行/字符分页共用格式索引；输出裁剪必须提供不跳字的结构化游标。
# 模块用途: 安全读取有界正文，准确区分完整文件、局部行和半行截断。
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from ..common.file_version import StaleFileVersionError, check_file_version, file_version
from ..common.text_file_window import (
    TextFileIndex,
    text_file_fingerprint,
    text_file_index,
    text_file_index_from_checkpoint,
)
from ..path_recovery_hints import suggest_workspace_typo_target
from ._filesystem_helpers import (
    _canonical_agent_final_report_target,
    _cursor_identity_is_valid,
    _cursor_source_changed,
    _CursorSourceChanged,
    _int_param,
    _internal_agent_status_ref,
    _readable_agent_final_report,
    _required_path,
    path_resolution_error_outcome,
)
from .filesystem_artifact_guard import (
    ToolOutputArtifactRedirectError,
    is_tool_output_artifact_path,
    tool_output_artifact_read_redirect,
    tool_output_artifact_typo_hint,
)
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .host_output_redaction import (
    credential_projection_receipt,
    redact_host_tool_outcome,
    redact_source_window,
)
from .models import ToolHandlerOutcome, output_policy_for_outcome


class TextReadSnapshot(Protocol):
    """已获准路径的文本读取视图；实现负责打开、读取后的快照一致性检查。"""

    total_chars: int
    total_lines: int

    # LLM: 行读取方用 with 关闭；实现保留打开时的快照一致性检查。
    # 函数用途: 打开本快照的文本流。
    def open(self) -> TextIO: ...
    # LLM: 字符窗口调用；实现负责有界读取和读后版本检查。
    # 函数用途: 返回指定字符页。
    def read(self, offset: int, limit: int) -> str: ...
    # LLM: 不一致须抛错，由原读取错误合同处理，不能返回空页冒充成功。
    # 函数用途: 核对快照是否仍有效。
    def check_current(self) -> None: ...


class TextSnapshotProvider(Protocol):
    """仅可信宿主可注入；路径、包装文件与版本门仍由外层执行。"""

    # LLM: 仅接收外层已获准路径；实现不替代路径、包装文件或文件版本门。
    # 函数用途: 为本次文本读取提供快照。
    def __call__(self, target: Path) -> TextReadSnapshot: ...


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
class TruncatedReadFooterRequest:
    total_lines: int
    next_start_line: int
    max_chars: int
    continuation_max_chars: int
    next_offset: int | None = None


# LLM: Keep ordinary file reads separate from registered tool-output wrappers; typed redirect
# errors must survive path resolution with their dedicated recovery contract.
# 函数用途: 校验 read_file 参数并读取普通文本；工具大输出则明确引导到 read_artifact。
def execute_read_file(tool, params: dict[str, Any], max_chars: int) -> ToolHandlerOutcome:
    try:
        raw_path = _required_path(params.get("path"))
        target = tool.resolve_path(raw_path)
    except ToolOutputArtifactRedirectError as exc:
        return ToolHandlerOutcome(
            "read_file",
            False,
            str(exc),
            error_code="TOOL_OUTPUT_REQUIRES_READ_ARTIFACT",
        )
    except ValueError as exc:
        return path_resolution_error_outcome("read_file", exc)
    outcome = _execute_read_file_request(ReadFileRequest(
        tool=tool,
        params=params,
        max_chars=max_chars,
        raw_path=raw_path,
        target=target,
    ))
    policy = getattr(tool, '_host_output_redaction', None)
    if policy is not None:
        snapshot = policy.snapshot
        withheld = any(status.source_path == target for status in snapshot.unavailable_sources)
        if withheld:
            outcome.output = '(凭据来源正文当前无法安全投影；原操作结果与来源坐标保留。)'
            outcome.result_envelope['credential_redaction'] = credential_projection_receipt(snapshot, withheld=True)
        canonical_policy = output_policy_for_outcome(tool.runtime_policy, outcome)
        outcome.result_envelope['tool_output_policy'] = {
            **outcome.result_envelope.get('tool_output_policy', {}),
            'trust': canonical_policy.trust, 'redaction': canonical_policy.redaction}
        return redact_host_tool_outcome(outcome, snapshot=snapshot,
            force_default=policy.force_default or target in snapshot.declared_source_paths)
    return outcome


# LLM: read_file may expose the exact host-generated child handoff report, but every other
# internal agent status ref remains blocked and must route through the typed lifecycle surface.
# 函数用途: 完成路径检查和安全分流后读取文件；只对精确 final_report 交接件开放内部目录例外。
def _execute_read_file_request(request: ReadFileRequest) -> ToolHandlerOutcome:
    target = request.target
    redirected_from = ""
    if not target.exists():
        canonical = _canonical_agent_final_report_target(target)
        if canonical is not None:
            try:
                canonical = request.tool.resolve_path(str(canonical))
            except ValueError:
                canonical = None
            if canonical is not None:
                redirected_from = str(target)
                request = ReadFileRequest(
                    tool=request.tool,
                    params=request.params,
                    max_chars=request.max_chars,
                    raw_path=request.raw_path,
                    target=canonical,
                )
                target = canonical
    if not target.exists():
        if isinstance(checkpoint := request.params.get("checkpoint"), dict):
            return _cursor_source_changed(
                "read_file", _CursorSourceChanged("source_unavailable", _checkpoint_last_line(checkpoint)),
            )
        typo_hint = _missing_tool_artifact_typo_hint(request.tool, request.raw_path)
        if typo_hint:
            return ToolHandlerOutcome(
                "read_file",
                False,
                typo_hint,
                error_code="TOOL_OUTPUT_REQUIRES_READ_ARTIFACT",
            )
        return missing_path_result(MissingPathRequest(
            tool_name="read_file",
            raw_path=request.raw_path,
            target=target,
            workspace_roots=request.tool.workspace_roots,
            display_path=request.tool.display_path(target),
            expected_kind="file",
            retry_tool="read_file",
            decide_access=request.tool.check_path_access,
        ))
    if not target.is_file():
        if isinstance(checkpoint := request.params.get("checkpoint"), dict):
            return _cursor_source_changed(
                "read_file", _CursorSourceChanged("file_id", _checkpoint_last_line(checkpoint)),
            )
        return _not_file_result(request.tool, target)
    if is_tool_output_artifact_path(target):
        return ToolHandlerOutcome(
            "read_file",
            False,
            tool_output_artifact_read_redirect(request.raw_path, target),
            error_code="TOOL_OUTPUT_REQUIRES_READ_ARTIFACT",
        )
    internal_ref = _internal_agent_status_ref(target)
    if internal_ref and not _readable_agent_final_report(target):
        return ToolHandlerOutcome(
            "read_file",
            False,
            json.dumps(internal_ref, ensure_ascii=False, indent=2),
            error_code="WRONG_STATUS_SURFACE",
        )
    try:
        version = file_version(target)
        outcome = _ordinary_file_result(request)
        if outcome.ok:
            check_file_version(target, version)
            outcome.result_envelope["file_version"] = version
            outcome.output += f"\nfile_version={version}"
    except StaleFileVersionError as exc:
        if checkpoint := request.params.get("checkpoint"):
            return _cursor_source_changed("read_file", _CursorSourceChanged("file_id", str(checkpoint["line"] - 1)))
        return ToolHandlerOutcome("read_file", False, str(exc), error_code="STALE_VERSION", retryable=True)
    except OSError as exc:
        return ToolHandlerOutcome("read_file", False, f"读取文件失败: {exc}", error_code="TOOL_EXECUTION_FAILED")
    if redirected_from:
        outcome.result_envelope["path_resolution"] = {
            "authority": "owner_agent_projection",
            "requested_path": redirected_from,
            "resolved_path": str(target),
        }
    return outcome


# LLM: 行读取不再 read_bytes 整文件；行/字符页共享解码失败说明，不猜格式、不自动转文件或发视觉请求。
# 函数用途: 返回可续读文本窗口；非文本结果引导模型选择已配置的合适能力。
def _ordinary_file_result(request: ReadFileRequest) -> ToolHandlerOutcome:
    target = request.target
    if _has_char_window_params(request.params):
        if "checkpoint" in request.params:
            return ToolHandlerOutcome(
                "read_file", False, "checkpoint 仅用于行续读，请省略 offset 和 start_char。未读取正文。",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        return _char_window_file_result(CharWindowFileRequest(
            tool=request.tool,
            target=target,
            params=request.params,
            default_max_chars=request.max_chars,
        ))
    try:
        return _stream_numbered_result(request)
    except _CursorSourceChanged as exc:
        return _cursor_source_changed("read_file", exc)
    except UnicodeDecodeError:
        return _non_text_file_result()
    except ValueError as exc:
        return ToolHandlerOutcome("read_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
    except OSError as exc:
        if isinstance(checkpoint := request.params.get("checkpoint"), dict):
            try:
                _read_decode_checkpoint(target, checkpoint)
            except _CursorSourceChanged as changed:
                return _cursor_source_changed("read_file", changed)
            except OSError:
                return _cursor_source_changed(
                    "read_file", _CursorSourceChanged("source_unavailable", _checkpoint_last_line(checkpoint)),
                )
        return ToolHandlerOutcome("read_file", False, f"读取文件失败: {exc}", error_code="TOOL_EXECUTION_FAILED")


def _checkpoint_last_line(checkpoint: dict[str, Any]) -> str:
    line = checkpoint.get("line")
    return str(line - 1) if type(line) is int and line > 0 else ""


def _read_decode_checkpoint(target: Path, checkpoint: object) -> dict[str, Any]:
    if (
        not isinstance(checkpoint, dict)
        or type(checkpoint.get("v")) is not int or checkpoint["v"] != 1
        or not _cursor_identity_is_valid(checkpoint.get("file_id"), 4)
        or type(checkpoint.get("line")) is not int or checkpoint["line"] < 1
        or type(checkpoint.get("byte_offset")) is not int
        or not 0 <= checkpoint["byte_offset"] <= checkpoint["file_id"][2]
        or not isinstance(checkpoint.get("encoding"), str) or len(checkpoint["encoding"]) > 100
    ):
        raise ValueError("checkpoint 字段或版本无效，请复制上一页完整的 decode_checkpoint。未读取正文。")
    if tuple(checkpoint["file_id"]) != text_file_fingerprint(target)[:4]:
        raise _CursorSourceChanged("file_id", str(checkpoint["line"] - 1))
    return checkpoint


# LLM: 每次 readline 自带长度上限；半行续读 offset 使用原正文坐标，不包含展示行号。
# 函数用途: 只保留请求行的有限文字，遇到超长行切换为字符游标，不跳过剩余部分。
def _stream_numbered_result(request: ReadFileRequest) -> ToolHandlerOutcome:
    params = request.params
    checkpoint = _read_decode_checkpoint(request.target, params["checkpoint"]) if "checkpoint" in params else None
    policy = getattr(request.tool, "_host_output_redaction", None)
    secrets = policy.snapshot.secret_values if policy is not None else ()
    lookbehind = max((len(value) for value in secrets if value), default=1) - 1
    provider = getattr(request.tool, "snapshot_provider", None)
    if provider is not None:
        index = provider(request.target)
    else:
        restored = (text_file_index_from_checkpoint(request.target, checkpoint, lookbehind=lookbehind)
                    if checkpoint is not None else None)
        index = restored if restored is not None else text_file_index(request.tool, request.target)
    start = _int_param(params.get("start_line"), name="start_line",
                       default=checkpoint["line"] if checkpoint else 1, min_value=1)
    if checkpoint is not None:
        if not isinstance(index, TextFileIndex):
            raise ValueError("当前文本来源无法复核 checkpoint，请传入 start_line。未读取正文。")
        if index.fingerprint[:4] != tuple(checkpoint["file_id"]) or index.encoding != checkpoint["encoding"]:
            raise _CursorSourceChanged("file_id_or_encoding", str(checkpoint["line"] - 1))
        if start != checkpoint["line"]:
            raise ValueError("start_line 必须等于 checkpoint.line，或省略 start_line。未读取正文。")
    end = _int_param(params.get("end_line"), name="end_line", default=max(index.total_lines, 1), min_value=1)
    if start > max(index.total_lines, 1) and params.get("end_line") is None:
        raise ValueError(_past_eof_line_message(start, index.total_lines))
    if end < start:
        raise ValueError("end_line 不能小于 start_line")
    if start > max(index.total_lines, 1):
        raise ValueError(_past_eof_line_message(start, index.total_lines))
    if not index.total_chars:
        return ToolHandlerOutcome("read_file", True, "(空文件)")
    limit = _line_max_chars(params, request.max_chars)
    rendered, used, number, offset, last = [], 0, 1, 0, start - 1
    with index.open() as handle:
        if isinstance(index, TextFileIndex):
            offset, number = index.seek_line(handle, start), start
            if checkpoint is not None and index.line_byte_offset(start, handle.tell()) != checkpoint["byte_offset"]:
                raise ValueError("checkpoint.byte_offset 与指定行边界不符，请复制上一页完整检查点。")
        else:
            while number < start:
                part = handle.readline(65536)
                if not part:
                    break
                offset += len(part)
                number += int(part.endswith("\n"))
        while number <= min(end, index.total_lines):
            previous_cookie = handle.tell() if isinstance(index, TextFileIndex) else 0
            part = handle.readline(limit + 1)
            part_chars = len(part)
            if secrets:
                part = redact_source_window(index.read, offset=offset, limit=len(part),
                    total_chars=index.total_chars, secret_values=secrets)
            prefix = f"{number}: "
            item = prefix + part.rstrip("\n")
            if used + int(bool(rendered)) + len(item) > limit:
                if rendered:
                    rendered.append(_truncated_read_footer(TruncatedReadFooterRequest(
                        index.total_lines, number, limit, request.max_chars)))
                    if isinstance(index, TextFileIndex):
                        handle.seek(previous_cookie)
                    break
                visible = max(1, limit - len(prefix))
                body = part[:visible]
                window = _char_read_window(offset, len(body), index.total_chars)
                footer = _truncated_read_footer(TruncatedReadFooterRequest(
                    index.total_lines, number, limit, request.max_chars, offset + len(body)))
                index.check_current()
                return ToolHandlerOutcome("read_file", True, f"{prefix}{body}\n{footer}",
                                          result_envelope={"read_window": window})
            rendered.append(item)
            used += len(item) + int(len(rendered) > 1)
            offset += part_chars
            last = number
            number += 1
        cookie = handle.tell() if isinstance(index, TextFileIndex) else 0
    window = _line_read_window(start, last, index.total_lines)
    envelope: dict[str, object] = {"read_window": window}
    if isinstance(index, TextFileIndex) and number <= index.total_lines:
        decode_checkpoint = index.checkpoint(line=number, chars=offset, cookie=cookie, lookbehind=lookbehind)
        envelope["decode_checkpoint"] = decode_checkpoint
        window["decode_checkpoint"] = decode_checkpoint
    index.check_current()
    return ToolHandlerOutcome("read_file", True, "\n".join(rendered), result_envelope=envelope)


def _not_file_result(tool, target: Path) -> ToolHandlerOutcome:
    if target.is_dir():
        display_path = tool.display_path(target)
        payload = {
            "ok": False,
            "error": "PATH_IS_DIRECTORY",
            "path": display_path,
            "message": "目标是目录，不是文件；请先用 list_files 查看目录，再读取具体文件。",
            "suggested_tool_call": {"tool": "list_files", "path": display_path, "max_depth": 1},
        }
        return ToolHandlerOutcome(
            "read_file",
            False,
            json.dumps(payload, ensure_ascii=False, indent=2),
            error_code="PATH_IS_DIRECTORY",
        )
    return ToolHandlerOutcome("read_file", False, f"目标不是文件: {tool.display_path(target)}", error_code="PATH_INVALID")


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


# LLM: 字符页复用有版本的编码/检查点索引；解码错误返回统一非文本说明，不落入 ValueError 参数分支。
# 函数用途: 有界读取文本并返回真实续读位置；失败不会触发额外模型或转换工具。
def _char_window_file_result(request: CharWindowFileRequest) -> ToolHandlerOutcome:
    try:
        offset, limit = _char_window_values(request.params, request.default_max_chars)
        provider = getattr(request.tool, "snapshot_provider", None)
        index = provider(request.target) if provider is not None else text_file_index(request.tool, request.target)
        total_chars = index.total_chars
        if offset > total_chars or (offset == total_chars and total_chars > 0):
            return _offset_out_of_range_result(offset, total_chars)
        policy = getattr(request.tool, '_host_output_redaction', None)
        window_limit = min(limit, request.default_max_chars)
        window = (redact_source_window(index.read, offset=offset, limit=window_limit,
            total_chars=total_chars, secret_values=policy.snapshot.secret_values)
            if policy is not None else index.read(offset, window_limit))
    except UnicodeDecodeError:
        return _non_text_file_result()
    except ValueError as exc:
        return ToolHandlerOutcome("read_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
    except OSError as exc:
        return ToolHandlerOutcome("read_file", False, f"读取文件失败: {exc}", error_code="TOOL_EXECUTION_FAILED")
    return _char_window_view_result(CharWindowView(
        offset=offset,
        window=window,
        total_chars=total_chars,
        requested_limit=limit,
        default_max_chars=request.default_max_chars,
    ))


# LLM: 只说明文本解码失败及能力发现途径；不凭扩展名断言格式，不承诺视觉工具已配置或自动转发私有文件。
# 函数用途: 给行/字符读取提供同一换路提示，避免把图片转换成另一图片后继续当文本读取。
def _non_text_file_result() -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "read_file", False,
        "文件不是有效文本（编码探测失败）；read_file 只能读取文本，重复读取不会获得图片或二进制内容。"
        "请按真实格式选择读取能力；如果是图片，可通过 tool_search 查找当前实际可用的图片或视觉工具，不能假定已配置。"
        "转换成另一种图片格式不会让它变成文本。若视觉能力不可用，请如实说明未做目视核验，"
        "使用已有文件或结构检查结果继续，不要反复确认同一文件是否存在。",
        error_code="TOOL_EXECUTION_FAILED",
    )


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


def _char_window_view_result(view: CharWindowView) -> ToolHandlerOutcome:
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
        return ToolHandlerOutcome(
            "read_file",
            True,
            f"{header}\n{view.window}\n{footer}",
            result_envelope={"read_window": _char_read_window(view.offset, len(view.window), view.total_chars)},
        )
    return ToolHandlerOutcome(
        "read_file",
        True,
        f"{header}\n{view.window}",
        result_envelope={"read_window": _char_read_window(view.offset, len(view.window), view.total_chars)},
    )


def _offset_out_of_range_result(offset: int, total_chars: int) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "read_file",
        False,
        f"offset 超出文件末尾：offset={offset}, total_chars={total_chars}。请改用更小的 offset。",
        error_code="OFFSET_OUT_OF_RANGE",
    )




def _missing_tool_artifact_typo_hint(tool, raw_path: str) -> str:
    suggested = suggest_workspace_typo_target(raw_path, tool.workspace_roots)
    if not suggested:
        return ""
    return tool_output_artifact_typo_hint(raw_path, tool.workspace_root, suggested)


def _char_read_window(offset: int, chars: int, total_chars: int) -> dict[str, int | bool | str]:
    next_offset = offset + chars
    return {
        "kind": "char_window",
        "offset": offset,
        "chars": chars,
        "next_offset": next_offset,
        "total_chars": total_chars,
        "complete": next_offset >= total_chars,
    }


def _line_read_window(start_line: int, end_line: int, total_lines: int) -> dict[str, Any]:
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
        footer = (
            f"... 已截断；PARTIAL view only; 这不是完整文件。 next_offset={request.next_offset}; "
            f"next_call=read_file(offset={request.next_offset}, max_chars={next_max_chars})。"
        )
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
