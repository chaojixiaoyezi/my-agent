
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
    # native 流式响应疑似被截断(message_stop 前 EOF 或 stop_reason=max_tokens 且参数 JSON
    # 未闭合)。截断把 write_file 参数清空 → 参数门报 TOOL_PARAMETER_REQUIRED 而非"截断"，
    # 过去落到没有恢复指令的分支。带上此标志，让"截断+write_file+缺参"也激活长内容恢复。
    truncated: bool = False


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
        f"- 只使用规范 write_file 调用，content 建议不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符；不要发明额外原文块协议。",
        "- 重新写同一路径的第一块可用 mode=\"overwrite\"；后续块必须用 mode=\"append\"。",
        "- 二进制产物用 write_file.data_base64。",
        "- 修改已有文件优先用 apply_patch。",
        "- 不要在普通回复或 JSON 参数里回传完整大文件正文。",
    ])
    return "\n".join(lines)


_TRUNCATION_WRITE_RECOVERY_CODES = {
    "TOOL_PARAMETER_REQUIRED",
    "TOOL_CALL_UNCLOSED",
    "TOOL_INLINE_CONTENT_STREAM_ABORTED",
}


def _needs_long_content_recovery(request: LongContentRecoveryRequest) -> bool:
    if request.result_ok:
        return False
    # 根因B 错配修复:native 长 content 写被截断 → 参数被清空 → 参数门报缺参(write_file +
    # TOOL_PARAMETER_REQUIRED)而非"截断"。仅当流确实疑似截断(truncated)时才认它，避免把
    # 模型纯粹漏填参数（非截断）也拽进长内容恢复。
    if request.truncated and _payload_tool(_request_payload(request)) == "write_file":
        error_code = str(request.result_error_code or "").strip()
        return error_code in _TRUNCATION_WRITE_RECOVERY_CODES
    return False


def _request_payload(request: LongContentRecoveryRequest) -> dict[str, object]:
    return request.payload if isinstance(request.payload, dict) else {}


def _payload_tool(payload: dict[str, object]) -> str:
    return str(payload.get("source_tool") or payload.get("tool_name") or payload.get("tool") or "").strip()


def _target_path(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    path = payload.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    return ""


__all__ = [
    "LongContentRecoveryRequest",
    "long_content_recovery_context",
]
