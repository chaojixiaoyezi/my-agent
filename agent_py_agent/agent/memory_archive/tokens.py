
from __future__ import annotations

"""conservative token estimation helpers for memory budgeting.

新手说明:
这里不是精确 tokenizer，也不假装精确。
它只给压缩前 hook、状态面板和预算提示一个偏保守的估算，避免中文、英文和工具大结果被严重低估。
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TurnTokenUsage:
    """Bundle for append_session_token_usage keyword parameters."""

    session_id: str
    turn_id: str
    input_tokens: int
    output_tokens: int
    tool_tokens: int
    created_at: str


@dataclass(frozen=True)
class TokenBudgetResult:

    status: str  # "ok" | "warning" | "block"
    current_tokens: int
    max_tokens: int
    ratio: float
    archive_level: int
    message: str


# 不同 archive level 的告警和阻断阈值
# level 越低，保留内容越多，预算越紧
_LEVEL_WARNING_RATIO = {0: 0.6, 1: 0.7, 2: 0.75, 3: 0.8}
_LEVEL_BLOCK_RATIO = {0: 0.85, 1: 0.9, 2: 0.95, 3: 1.0}


def check_token_budget(
    current_tokens: int,
    max_tokens: int,
    archive_level: int = 3,
) -> TokenBudgetResult:

    level = max(0, min(3, int(archive_level) if not isinstance(archive_level, bool) else 3))
    if max_tokens <= 0:
        return TokenBudgetResult(
            status="ok",
            current_tokens=current_tokens,
            max_tokens=max_tokens,
            ratio=0.0,
            archive_level=level,
            message="max_tokens 未设置，跳过预算检查。",
        )
    ratio = current_tokens / max_tokens
    warning_threshold = _LEVEL_WARNING_RATIO.get(level, 0.75)
    block_threshold = _LEVEL_BLOCK_RATIO.get(level, 0.95)
    if ratio >= block_threshold:
        status = "block"
        msg = f"token 预算已超限（{current_tokens}/{max_tokens}，{ratio:.0%}），archive level={level}，必须压缩。"
    elif ratio >= warning_threshold:
        status = "warning"
        msg = f"token 预算接近上限（{current_tokens}/{max_tokens}，{ratio:.0%}），archive level={level}，建议压缩。"
    else:
        status = "ok"
        msg = f"token 预算正常（{current_tokens}/{max_tokens}，{ratio:.0%}），archive level={level}。"
    return TokenBudgetResult(
        status=status,
        current_tokens=current_tokens,
        max_tokens=max_tokens,
        ratio=ratio,
        archive_level=level,
        message=msg,
    )


def estimate_tokens(payload: Any) -> int:

    text = _payload_to_text(payload)
    if not text:
        return 1

    cjk_chars = sum(1 for char in text if _is_cjk(char))
    non_cjk_chars = len(text) - cjk_chars
    utf8_bytes = len(text.encode("utf-8"))
    structured_overhead = _structured_overhead(payload)

    cjk_estimate = cjk_chars + math.ceil(non_cjk_chars / 4)
    byte_estimate = math.ceil(utf8_bytes / 3)
    dense_text_estimate = math.ceil(len(text) / 3)

    return max(1, cjk_estimate, byte_estimate, dense_text_estimate) + structured_overhead


def token_ledger_dir(root: str | Path) -> Path:

    return Path(root) / "memory_archive" / "tokens"


def append_session_token_usage(
    root: str | Path,
    *,
    usage: TurnTokenUsage,
) -> dict[str, Any]:

    path = token_ledger_dir(root) / f"{usage.session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}
    turns = payload.get("turns", [])
    if not isinstance(turns, list):
        turns = []
    turn_total = max(0, int(usage.input_tokens)) + max(0, int(usage.output_tokens)) + max(0, int(usage.tool_tokens))
    turns.append(
        {
            "turn_id": str(usage.turn_id),
            "created_at": str(usage.created_at),
            "input_tokens": max(0, int(usage.input_tokens)),
            "output_tokens": max(0, int(usage.output_tokens)),
            "tool_tokens": max(0, int(usage.tool_tokens)),
            "turn_total": turn_total,
        }
    )
    cumulative = sum(int(item.get("turn_total", 0) or 0) for item in turns)
    written = {
        "session_id": str(usage.session_id),
        "turn_count": len(turns),
        "cumulative_tokens": cumulative,
        "turns": turns,
    }
    path.write_text(json.dumps(written, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "path": str(path),
        "session_id": str(usage.session_id),
        "turn_id": str(usage.turn_id),
        "turn_total": turn_total,
        "cumulative_tokens": cumulative,
        "turn_count": len(turns),
    }


def _payload_to_text(payload: Any) -> str:

    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _structured_overhead(payload: Any) -> int:

    if isinstance(payload, dict):
        return max(1, len(payload) // 2)
    if isinstance(payload, list | tuple | set):
        return max(1, len(payload) // 4)
    return 0


def _is_cjk(char: str) -> bool:

    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )
