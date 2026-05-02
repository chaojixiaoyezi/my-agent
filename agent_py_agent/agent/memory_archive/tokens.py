from __future__ import annotations

"""LLM: conservative token estimation helpers for memory budgeting.

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
class TokenBudgetResult:
    """LLM: result of a token budget check against configured limits.

    新手说明:
    检查 token 预算后返回的状态对象。
    `status` 有三种：ok（正常）、warning（接近上限）、block（已超限）。
    `ratio` 是当前 token 占最大值的比例，0.0-1.0+。
    `message` 是给人看的提示。
    """

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
    """LLM: check whether current token usage is within budget for the given archive level.

    新手说明:
    不同 archive level 有不同的告警和阻断阈值。
    level 0（全量归档）最紧，因为存的内容多、消耗快；level 3（最小恢复）最松。
    返回值里有 status 和 ratio，调用方可以据此决定是否触发压缩或提示用户。

    参数说明:
    `current_tokens` 是当前累计 token 估算；`max_tokens` 是配置的最大 token 上限；
    `archive_level` 是 0-3 的归档等级。

    返回说明:
    返回 TokenBudgetResult，包含状态、比例和提示信息。
    """

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
    """LLM: estimate token count conservatively without provider-specific tokenizers.

    新手说明:
    真正 token 数要看模型 tokenizer。
    这里用字符、UTF-8 字节、中文字符和英文片段一起估算，宁愿稍微多算一点，也不要把上下文快满这件事看轻。

    参数说明:
    `payload` 可以是字符串、字典、列表或任何可转成字符串的对象。

    返回说明:
    返回至少为 1 的整数 token 估算值。
    """

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
    """LLM: return the session token ledger directory used by compression budgeting.

    新手说明:
    每轮 token 预算要能累计到 session 级别，所以单独放一个目录存账本。
    """

    return Path(root) / "memory_archive" / "tokens"


def append_session_token_usage(
    root: str | Path,
    *,
    session_id: str,
    turn_id: str,
    input_tokens: int,
    output_tokens: int,
    tool_tokens: int,
    created_at: str,
) -> dict[str, Any]:
    """LLM: append one turn token estimate to the session ledger and return cumulative totals.

    新手说明:
    这里不追求数据库复杂度，只需要一个稳定 JSON 文件，让压缩判断知道"到目前为止大概用了多少"。
    """

    path = token_ledger_dir(root) / f"{session_id}.json"
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
    turn_total = max(0, int(input_tokens)) + max(0, int(output_tokens)) + max(0, int(tool_tokens))
    turns.append(
        {
            "turn_id": str(turn_id),
            "created_at": str(created_at),
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "tool_tokens": max(0, int(tool_tokens)),
            "turn_total": turn_total,
        }
    )
    cumulative = sum(int(item.get("turn_total", 0) or 0) for item in turns)
    written = {
        "session_id": str(session_id),
        "turn_count": len(turns),
        "cumulative_tokens": cumulative,
        "turns": turns,
    }
    path.write_text(json.dumps(written, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "path": str(path),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "turn_total": turn_total,
        "cumulative_tokens": cumulative,
        "turn_count": len(turns),
    }


def _payload_to_text(payload: Any) -> str:
    """LLM: convert any payload into text before token estimation.

    新手说明:
    估算 token 前先把结构化对象变成 JSON 字符串；如果 JSON 序列化失败，就退回 `str()`。

    参数说明:
    `payload` 是待估算对象。

    返回说明:
    返回可用于字符统计的字符串。
    """

    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _structured_overhead(payload: Any) -> int:
    """LLM: add a small overhead for structured containers.

    新手说明:
    JSON 结构里的字段名、括号和分隔符也会消耗 token。这里按字段/元素数量补一点预算。

    参数说明:
    `payload` 是原始对象。

    返回说明:
    返回额外 token 估算值。
    """

    if isinstance(payload, dict):
        return max(1, len(payload) // 2)
    if isinstance(payload, list | tuple | set):
        return max(1, len(payload) // 4)
    return 0


def _is_cjk(char: str) -> bool:
    """LLM: detect whether one character is in common CJK ranges.

    新手说明:
    中文字符通常不能按英文"四字符一个 token"粗算，所以单独统计。

    参数说明:
    `char` 是单个字符。

    返回说明:
    中文/日文/韩文常见汉字范围内返回 True，否则 False。
    """

    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )
