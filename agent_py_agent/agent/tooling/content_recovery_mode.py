
from __future__ import annotations

from dataclasses import dataclass

from .content_transport_policy import RECOVERY_WRITE_CHUNK_CHARS


@dataclass(frozen=True)
class LongContentRecoveryRequest:
    payload: object
    result_tool: str
    result_ok: bool
    output: str
    result_error_code: str = ""


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
        "- 文本完整文件可用独立成行的 [WRITE_FILE_RAW path=\"...\"]...[/WRITE_FILE_RAW] 原文块；"
        "不要把 WRITE_FILE_RAW 当 JSON tool 名。二进制产物用 write_file.data_base64。",
        f"- 如果继续用 write_file.content，每次 content 不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符。",
        "- 修改已有文件优先用 apply_patch。",
        "- 不要在普通回复或 JSON 参数里回传完整大文件正文。",
    ])
    return "\n".join(lines)


def _needs_long_content_recovery(request: LongContentRecoveryRequest) -> bool:
    output = request.output
    if request.result_ok:
        return False
    if request.result_tool == "__parse_error__":
        return _parse_error_mentions_long_write(request.payload, request.result_error_code)
    return False


def _parse_error_mentions_long_write(payload: object, result_error_code: str) -> bool:
    if not isinstance(payload, dict):
        return False
    error_code = str(result_error_code or payload.get("error_code") or "").strip()
    if error_code not in {"TOOL_CALL_UNCLOSED", "TOOL_INLINE_CONTENT_STREAM_ABORTED"}:
        return False
    return _payload_tool(payload) == "write_file" and _payload_has_content_field(payload)


def _payload_tool(payload: dict[str, object]) -> str:
    return str(payload.get("source_tool") or payload.get("tool_name") or "").strip()


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
