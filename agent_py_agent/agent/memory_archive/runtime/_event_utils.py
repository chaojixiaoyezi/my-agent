# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

"""Pure utility helpers for runtime raw archive event builders."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

_PREVIEW_LIMITS = {
    0: 2048,
    1: 1024,
    2: 512,
    3: 160,
}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summarize_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summarize text 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summarize_text(content: str, *, fallback: str, limit: int = 96) -> str:
    """Build a short deterministic summary when full previews should not be stored."""
    compact = " ".join(str(content).split())
    if not compact:
        return fallback
    limit = max(0, int(limit))
    short = compact[:limit]
    if len(compact) > limit:
        short += "..."
    return short


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _normalize_tool_call 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize tool call 涉及的字段，让后续匹配和存储使用同一形态。
def _normalize_tool_call(call: Any) -> dict[str, Any]:
    """Coerce arbitrary tool-call objects into a plain dictionary."""
    if isinstance(call, Mapping):
        return dict(call)
    if is_dataclass(call) and not isinstance(call, type):
        return asdict(call)
    if hasattr(call, "__dict__"):
        return {key: value for key, value in vars(call).items() if not key.startswith("_")}
    return {"value": str(call)}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _normalize_archive_level 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize archive level 涉及的字段，让后续匹配和存储使用同一形态。
def _normalize_archive_level(value: int) -> int:
    """Keep archive levels inside the supported 0..3 range."""
    if isinstance(value, bool):
        return 3
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 3
    if level < 0 or level > 3:
        return 3
    return level


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _preview 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 preview 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _preview(content: str, archive_level: int, preview_limits: dict[int, int] | None = None) -> str:
    """Trim archived text according to archive level."""
    limits = preview_limits or _PREVIEW_LIMITS
    limit = int(limits.get(_normalize_archive_level(archive_level), _PREVIEW_LIMITS[3]))
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return f"{content[: limit - 3]}..."


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _event_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 event id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _event_id(payload: Mapping[str, Any]) -> str:
    """Derive a stable raw event id from canonical payload facts."""
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"raw:{digest[:32]}"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _content_hash 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 content hash 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _content_hash(content: str) -> str:
    """Compute a SHA-256 content hash with an explicit prefix."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _canonical_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 canonical json 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _canonical_json(payload: Any) -> str:
    """Serialize payloads into deterministic JSON for identity hashes."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _stable_display_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 stable display json 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _stable_display_json(payload: Any) -> str:
    """Serialize metadata for human-facing previews while keeping input order."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"), default=str)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _first_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 first text 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    """Find the first non-empty text value among candidate keys."""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _first_bool 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 first bool 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _first_bool(payload: Mapping[str, Any], *keys: str) -> bool | None:
    """Find the first boolean-ish status among candidate keys."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        parsed = _bool_from_text(value) if isinstance(value, str) else None
        if parsed is not None:
            return parsed
    return None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _bool_from_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bool from text 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _bool_from_text(value: str) -> bool | None:
    text = value.strip().lower()
    if text in {"true", "1", "yes", "ok", "success"}:
        return True
    if text in {"false", "0", "no", "error", "failed", "failure"}:
        return False
    return None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _tool_status 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 tool status 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _tool_status(payload: Mapping[str, Any], tool_success: bool | None) -> str:
    """Choose a stable tool status string."""
    status = _first_text(payload, "status")
    if status:
        return status
    if tool_success is True:
        return "ok"
    if tool_success is False:
        return "error"
    return "unknown"
