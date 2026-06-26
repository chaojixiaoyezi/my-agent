
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .content_transport_policy import (
    RECOVERY_STREAMING_INLINE_WRITE_ABORT_CHARS,
    RECOVERY_WRITE_CHUNK_CHARS,
    streaming_inline_write_abort_limit,
)


@dataclass(frozen=True)
class LongContentRecoveryRequest:
    payload: object
    result_tool: str
    result_ok: bool
    output: str
    result_error_code: str = ""
    # native 流式响应疑似被截断(message_stop 前 EOF 或 stop_reason=max_tokens 且参数 JSON
    # 未闭合)。截断把 write_file 参数清空 → 参数门报 TOOL_PARAMETER_REQUIRED 而非"截断"，
    # 过去落到没有恢复指令的分支。带上此标志，让"截断+write_file+缺参"也激活长内容恢复。
    truncated: bool = False


@dataclass(frozen=True)
class LongContentRecoveryState:
    active: bool = False
    target_path: str = ""
    max_chunk_chars: int = RECOVERY_WRITE_CHUNK_CHARS
    max_inline_chars: int = RECOVERY_STREAMING_INLINE_WRITE_ABORT_CHARS
    source_error_code: str = ""


def long_content_recovery_context(request: LongContentRecoveryRequest) -> str:
    if not _needs_long_content_recovery(request):
        return ""
    target_path = _target_path(request.payload)
    lines = [
        "[tool-system]",
        "long_content_recovery_mode: active",
        "reason: previous write_file content was too long, truncated, or rejected.",
    ]
    if target_path:
        lines.append(f"target_path: {target_path}")
    lines.extend([
        "rules:",
        "- 下一轮只输出 1 个完整机器写入块，闭合后等待工具结果；不要同时输出解释正文。",
        f"- 优先使用独立成行的 [WRITE_FILE_RAW path=\"...\" mode=\"append\"] 原文块，正文建议不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符；不要把 WRITE_FILE_RAW 当 JSON tool 名。",
        f"- 如果使用 JSON write_file，content 也建议不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符；完整闭合的中等长度块会先交给工具层处理。",
        "- 重新写同一路径的第一块可用 mode=\"overwrite\"；后续块必须用 mode=\"append\"。",
        "- 二进制产物用 write_file.data_base64。",
        "- 修改已有文件优先用 apply_patch。",
        "- 不要在普通回复或 JSON 参数里回传完整大文件正文。",
    ])
    return "\n".join(lines)


def long_content_recovery_state_from_records(records: object) -> LongContentRecoveryState:
    """Return active recovery state from structured archived tool records."""

    if not isinstance(records, list):
        return LongContentRecoveryState()
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        payload = _record_payload(record)
        error_code = str(record.get("error_code") or payload.get("error_code") or "").strip()
        result_tool = str(record.get("tool") or payload.get("tool") or "").strip()
        if _parse_error_mentions_long_write(payload, error_code):
            return LongContentRecoveryState(
                active=True,
                target_path=_target_path(payload),
                source_error_code=error_code or result_tool,
            )
    return LongContentRecoveryState()


def long_content_recovery_payload_too_large(
    payload: object,
    state: LongContentRecoveryState,
    *,
    max_inline_chars: int | None = None,
) -> bool:
    if not state.active or not isinstance(payload, dict):
        return False
    if _payload_tool(payload) != "write_file":
        return False
    content = payload.get("content")
    hard_limit = _recovery_inline_hard_limit(state, max_inline_chars)
    if not isinstance(content, str) or len(content) <= hard_limit:
        return False
    target = _target_path(payload)
    if state.target_path and target and target != state.target_path:
        return False
    return True


def long_content_recovery_block_context(
    payload: object,
    state: LongContentRecoveryState,
    *,
    max_inline_chars: int | None = None,
) -> str:
    target = _target_path(payload) or state.target_path
    actual = len(payload.get("content") or "") if isinstance(payload, dict) else 0
    hard_limit = _recovery_inline_hard_limit(state, max_inline_chars)
    lines = [
        "[tool-system]",
        "long_content_recovery_mode: blocked_large_write",
        f"target_path: {target}",
        f"content_chars: {actual}",
        f"max_chunk_chars: {state.max_chunk_chars}",
        f"max_inline_chars: {hard_limit}",
        "reason: current write_file content exceeds the structured recovery inline hard limit.",
        "rules:",
        f"- 输出 1 个完整闭合的机器写入块，正文建议不超过 {state.max_chunk_chars} 字符，硬上限 {hard_limit} 字符。",
        "- 优先用独立成行的 WRITE_FILE_RAW 原文块；也可以用 write_file JSON 小块。",
        "- 第一块可用 mode=\"overwrite\" 重启同一路径，后续块必须用 mode=\"append\"。",
        "- 每块完整闭合后（WRITE_FILE_RAW 用 [/WRITE_FILE_RAW] 收尾；JSON write_file 是一个完整工具调用）等待工具结果，不要一次塞完整报告正文。",
    ]
    return "\n".join(lines)


def _recovery_inline_hard_limit(state: LongContentRecoveryState, max_inline_chars: int | None) -> int:
    if max_inline_chars is None:
        return max(state.max_chunk_chars, state.max_inline_chars)
    return max(state.max_chunk_chars, streaming_inline_write_abort_limit(max_inline_chars))


_TRUNCATION_WRITE_RECOVERY_CODES = {
    "TOOL_PARAMETER_REQUIRED",
    "TOOL_CALL_UNCLOSED",
    "TOOL_INLINE_CONTENT_STREAM_ABORTED",
}


def _needs_long_content_recovery(request: LongContentRecoveryRequest) -> bool:
    if request.result_ok:
        return False
    if request.result_tool == "__parse_error__":
        return _parse_error_mentions_long_write(request.payload, request.result_error_code)
    # 根因B 错配修复:native 长 content 写被截断 → 参数被清空 → 参数门报缺参(write_file +
    # TOOL_PARAMETER_REQUIRED)而非"截断"。仅当流确实疑似截断(truncated)时才认它，避免把
    # 模型纯粹漏填参数（非截断）也拽进长内容恢复。
    if request.truncated and _payload_tool(_request_payload(request)) == "write_file":
        error_code = str(request.result_error_code or "").strip()
        return error_code in _TRUNCATION_WRITE_RECOVERY_CODES
    return False


def _request_payload(request: LongContentRecoveryRequest) -> dict[str, object]:
    return request.payload if isinstance(request.payload, dict) else {}


def _parse_error_mentions_long_write(payload: object, result_error_code: str) -> bool:
    if not isinstance(payload, dict):
        return False
    if _payload_has_write_recovery(payload):
        return True
    error_code = str(result_error_code or payload.get("error_code") or "").strip()
    if error_code not in {"TOOL_CALL_UNCLOSED", "TOOL_INLINE_CONTENT_STREAM_ABORTED"}:
        return False
    return _payload_tool(payload) == "write_file" and _payload_has_content_field(payload)


def _payload_has_write_recovery(payload: dict[str, object]) -> bool:
    recovery = payload.get("write_recovery")
    if not isinstance(recovery, dict):
        return False
    return str(recovery.get("strategy") or "").strip() == "restart_same_file_with_append_chunks"


def _payload_tool(payload: dict[str, object]) -> str:
    return str(payload.get("source_tool") or payload.get("tool_name") or payload.get("tool") or "").strip()


def _payload_has_content_field(payload: dict[str, object]) -> bool:
    if payload.get("content_field_present") is True:
        return True
    if payload.get("content") is not None:
        return True
    return False


def _target_path(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    path = payload.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    return ""


def _record_payload(record: dict[str, Any]) -> dict[str, object]:
    payload = record.get("parameters")
    if isinstance(payload, dict):
        return payload
    payload = record.get("payload")
    if isinstance(payload, dict):
        return payload
    return record


__all__ = [
    "LongContentRecoveryRequest",
    "LongContentRecoveryState",
    "long_content_recovery_block_context",
    "long_content_recovery_context",
    "long_content_recovery_payload_too_large",
    "long_content_recovery_state_from_records",
]
