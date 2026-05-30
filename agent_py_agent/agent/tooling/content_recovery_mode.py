# LLM: Long content recovery mode converts repeated large-payload failures into a compact next-turn policy.
# 模块用途: 当 write_file 的大正文导致解析失败或被拒绝时，给下一轮模型追加统一降级策略。

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .content_transport_policy import RECOVERY_WRITE_CHUNK_CHARS


# LLM: LongContentRecoveryRequest is the bundle ToolLoopService passes after one tool call completes.
# 类用途: 保存判断是否进入长内容恢复模式所需的工具 payload、执行结果和输出摘要。
@dataclass(frozen=True)
class LongContentRecoveryRequest:
    payload: object
    result_tool: str
    result_ok: bool
    output: str


# LLM: long_content_recovery_context returns a tiny tool-system instruction for the next model turn.
# 函数用途: 识别长正文截断/拒绝场景；需要恢复时生成稳定规则，不需要时返回空字符串。
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


# LLM: _needs_long_content_recovery keeps detection conservative so normal tool failures are unaffected.
# 函数用途: 只把 write_file 的长正文截断、解析失败或 inline 上限拒绝切入恢复模式。
def _needs_long_content_recovery(request: LongContentRecoveryRequest) -> bool:
    output = request.output
    if request.result_ok:
        return False
    if request.result_tool == "__parse_error__":
        return _parse_error_mentions_long_write(request.payload, output)
    if request.result_tool == "write_file":
        return "inline content 过长" in output or "inline content 超过推荐值" in output
    return False


# LLM: _parse_error_mentions_long_write recognizes truncated write payloads without retaining raw bodies.
# 函数用途: 判断解析错误是否来自 write_file 的 content 被截断或过长。
def _parse_error_mentions_long_write(payload: object, output: str) -> bool:
    text = f"{_payload_text(payload)}\n{output}"
    if "write_file" not in text:
        return False
    return "content" in text and any(
        marker in text
        for marker in ("缺少结束标记", "太长", "截断", "分块追加", "不超过")
    )


# LLM: _target_path extracts a human-readable path from either parsed payloads or incomplete raw JSON.
# 函数用途: 从工具参数或 parse error raw 里取目标路径，让恢复提示能继续围绕同一个文件。
def _target_path(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    path = payload.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    raw = str(payload.get("raw") or "")
    return _raw_path(raw)


# LLM: _raw_path keeps parse-error path recovery literal and bounded.
# 函数用途: 在不完整 JSON 文本里抓取 path 字段；只返回短路径，避免把原始大正文带回 prompt。
def _raw_path(raw: str) -> str:
    match = re.search(r'"path"\s*:\s*"([^"]{1,240})"', raw)
    if not match:
        return ""
    return match.group(1).strip()


# LLM: _payload_text gives detectors a small normalized string without leaking full content values.
# 函数用途: 从 payload 中提取 tool/error/raw/path 等小字段供规则判断，避免长 content 反向进入上下文。
def _payload_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in ("tool", "error", "raw", "path"):
        value = payload.get(key)
        if value is not None:
            parts.append(_bounded_text(value))
    return "\n".join(parts)


# LLM: _bounded_text prevents pathological tool parameters from bloating recovery detection.
# 函数用途: 把任意值转成最多一小段文本；检测用，不作为事实或产物保存。
def _bounded_text(value: Any, limit: int = 1000) -> str:
    text = str(value)
    return text[:limit]
