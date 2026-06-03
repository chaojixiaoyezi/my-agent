
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .content_transport_policy import RECOVERY_WRITE_CHUNK_CHARS


@dataclass(frozen=True)
class LongContentRecoveryRequest:
    payload: object
    result_tool: str
    result_ok: bool
    output: str


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
        "- 文本完整文件可用 [WRITE_FILE_RAW path=\"...\"]...[/WRITE_FILE_RAW] 原文块；"
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
        return _parse_error_mentions_long_write(request.payload, output)
    if request.result_tool == "write_file":
        return "inline content 过长" in output or "inline content 超过推荐值" in output
    return False


def _parse_error_mentions_long_write(payload: object, output: str) -> bool:
    text = f"{_payload_text(payload)}\n{output}"
    if "write_file" not in text:
        return False
    return "content" in text and any(
        marker in text
        for marker in ("缺少结束标记", "太长", "截断", "分块追加", "不超过")
    )


def _target_path(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    path = payload.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    raw = str(payload.get("raw") or "")
    return _raw_path(raw)


def _raw_path(raw: str) -> str:
    match = re.search(r'"path"\s*:\s*"([^"]{1,240})"', raw)
    if not match:
        return ""
    return match.group(1).strip()


def _payload_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in ("tool", "error", "raw", "path"):
        value = payload.get(key)
        if value is not None:
            parts.append(_bounded_text(value))
    return "\n".join(parts)


def _bounded_text(value: Any, limit: int = 1000) -> str:
    text = str(value)
    return text[:limit]
