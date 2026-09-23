# LLM: 本模块拥有唯一估算口径及用量账；有界小对象直接编码，其余流式统计，不改变UTF8/字符上界、结构开销或异常回退，联测容量调用方。
# 模块用途: 为记忆及模型输入提供保守估算和账本记录；估算不是供应商实耗，读取估算本身不写账。

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

# 小对象复用公开直接编码入口，避免高频窗口试算积累流式编码器闭包；大对象仍不复制整份JSON。
_SMALL_JSON_MAX_BYTES = 512 * 1024
_SMALL_JSON_MAX_DEPTH = 64


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


# LLM: 同一sort/default JSON序列按结构上界选择有界直接编码或流式累计；估算数值及异常回退不变，最大单值/字典排序仍需内存。
# 函数用途: 有界估算输入，避免大历史副本和高频小请求的编码器积累，保持既有预算口径，不发请求或写账。
def estimate_tokens(payload: Any) -> int:
    chars, utf8_bytes = _payload_lengths(payload)
    if not chars:
        return 1
    return max(1, math.ceil(utf8_bytes / 3), math.ceil(chars / 3)) + _structured_overhead(payload)


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


# LLM: 仅可证明有界的内置结构使用公开dumps；其它仍流式。先完成JSON再裁决UTF8错误，后续JSON失败必须保留旧str回退优先级。
# 函数用途: 统计字符及UTF8字节；小请求避免反复创建流式编码器，大来源不另复制全文，不调用额外对象转换或写账。
def _payload_lengths(payload: Any) -> tuple[int, int]:
    if isinstance(payload, str):
        return len(payload), sum(len(payload[offset:offset + 8192].encode("utf-8")) for offset in range(0, len(payload), 8192))
    if _bounded_json_size(payload, _SMALL_JSON_MAX_BYTES) is not None:
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(payload)
        return len(text), len(text.encode("utf-8"))
    chars = utf8_bytes = 0
    encoding_error = None
    parts = json.JSONEncoder(ensure_ascii=False, sort_keys=True, default=str).iterencode(payload)
    while True:
        try:
            part = next(parts)
        except StopIteration:
            if encoding_error is not None:
                raise encoding_error from None
            return chars, utf8_bytes
        except (TypeError, ValueError):
            text = str(payload)
            return len(text), len(text.encode("utf-8"))
        chars += len(part)
        if encoding_error is None:
            try:
                utf8_bytes += sum(len(part[offset:offset + 8192].encode("utf-8")) for offset in range(0, len(part), 8192))
            except UnicodeEncodeError as exc:
                encoding_error = exc.with_traceback(None)


# LLM: 只读精确内置类型，不调用自定义转换或比较；返回JSON UTF8保守上界，未知/环/深树/超限仅退回原流式口径，不拒绝输入。
# 函数用途: 证明一次直接编码的副本有界；按最坏转义、分隔符和数字长度计量，不序列化正文或修改容器。
def _bounded_json_size(
    payload: Any, remaining: int, active: set[int] | None = None, depth: int = 0,
) -> int | None:
    if remaining < 0 or depth >= _SMALL_JSON_MAX_DEPTH:
        return None
    kind = type(payload)
    if kind is str:
        size = 6 * len(payload) + 2
        return size if size <= remaining else None
    if payload is None or kind is bool:
        return 5 if remaining >= 5 else None
    if kind is int:
        size = payload.bit_length() + 2
        return size if size <= remaining else None
    if kind is float:
        return 32 if remaining >= 32 else None
    if kind is list or kind is tuple or kind is dict:
        return _bounded_json_container_size(payload, remaining, active, depth)
    return None


# LLM: 调用方已验证精确内置容器；只跟踪当前祖先防环，共享子对象按每次出现计量，所有返回路径都释放本层身份。
# 函数用途: 累计容器标点、键与值的上界，超限或不能证明时立即停止，不复制整份容器或修改来源。
def _bounded_json_container_size(
    payload: list | tuple | dict, remaining: int, active: set[int] | None, depth: int,
) -> int | None:
    active = set() if active is None else active
    identity = id(payload)
    is_dict = type(payload) is dict
    size = 2 + (6 if is_dict else 2) * len(payload)
    if identity in active or size > remaining:
        return None
    active.add(identity)
    try:
        parts = (item for pair in payload.items() for item in pair) if is_dict else payload
        for part in parts:
            item_size = _bounded_json_size(part, remaining - size, active, depth + 1)
            if item_size is None:
                return None
            size += item_size
        return size
    finally:
        active.remove(identity)


def _structured_overhead(payload: Any) -> int:

    if isinstance(payload, dict):
        return max(1, len(payload) // 2)
    if isinstance(payload, list | tuple | set):
        return max(1, len(payload) // 4)
    return 0
