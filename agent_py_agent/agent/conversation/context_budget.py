# LLM: Background context budgets keep long-lived threads prompt-safe without dropping refs.
# 模块用途: 对后台主代理上下文做通用裁剪，只保留轻量摘要和引用，避免长会话/代理树撑爆 prompt。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackgroundContextBudget:
    """Prompt budget knobs for durable background wakeups.

    These limits are not task-specific quality gates. They only cap how much
    local ledger content is inlined into one background prompt; full details stay
    in the underlying message, observation, case, and artifact files.
    """

    max_string_chars: int = 1200
    max_list_items: int = 20
    max_dict_items: int = 80
    max_depth: int = 6


DEFAULT_BACKGROUND_CONTEXT_BUDGET = BackgroundContextBudget()


# LLM: bounded_background_context_payload trims only prompt copies; durable stores remain untouched.
# 函数用途: 生成后台主代理 prompt 使用的 bounded context payload。
def bounded_background_context_payload(
    *,
    bundle: dict[str, Any],
    pending_wake_signals: list[dict[str, Any]],
    agent_tree: dict[str, Any],
    budget: BackgroundContextBudget | None = None,
) -> dict[str, Any]:
    limits = budget or DEFAULT_BACKGROUND_CONTEXT_BUDGET
    return {
        "thread": _bounded_value(bundle.get("thread"), limits),
        "messages": [_bounded_message(item, limits) for item in _list(bundle.get("messages"))],
        "tasks": [_bounded_value(item, limits) for item in _list(bundle.get("tasks"))],
        "channel_bindings": [
            _bounded_value(item, limits) for item in _list(bundle.get("channel_bindings"))
        ],
        "observations": [
            _bounded_observation(item, limits) for item in _list(bundle.get("observations"))
        ],
        "pending_wake_signals": [
            _bounded_observation(item, limits) for item in _list(pending_wake_signals)
        ],
        "agent_tree": _bounded_value(agent_tree, limits),
    }


def _bounded_message(value: object, budget: BackgroundContextBudget) -> dict[str, Any]:
    row = dict(value) if isinstance(value, dict) else {}
    content = str(row.get("content") or "")
    preview = _clip(content, budget.max_string_chars)
    row["content"] = preview
    if preview != content:
        row["content_truncated"] = True
        row["content_original_chars"] = len(content)
    return _bounded_value(row, budget)


def _bounded_observation(value: object, budget: BackgroundContextBudget) -> dict[str, Any]:
    row = dict(value) if isinstance(value, dict) else {}
    for key in ("summary", "reason", "content"):
        _clip_observation_field(row, key, budget.max_string_chars)
    return _bounded_value(row, budget)


def _clip_observation_field(row: dict[str, Any], key: str, limit: int) -> None:
    if key not in row:
        return
    text = str(row.get(key) or "")
    preview = _clip(text, limit)
    row[key] = preview
    if preview != text:
        row[f"{key}_truncated"] = True
        row[f"{key}_original_chars"] = len(text)


def _bounded_value(value: object, budget: BackgroundContextBudget, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        clipped = _clip(value, budget.max_string_chars)
        if clipped == value:
            return value
        return {
            "preview": clipped,
            "truncated": True,
            "original_chars": len(value),
        }
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= budget.max_depth:
        return {
            "truncated": True,
            "reason": "max_depth",
            "type": type(value).__name__,
        }
    if isinstance(value, dict):
        items = list(value.items())
        bounded = {
            str(key): _bounded_value(item, budget, depth=depth + 1)
            for key, item in items[: budget.max_dict_items]
        }
        if len(items) > budget.max_dict_items:
            bounded["_truncated_dict_items"] = len(items) - budget.max_dict_items
        return bounded
    if isinstance(value, (list, tuple)):
        items = list(value)
        bounded = [_bounded_value(item, budget, depth=depth + 1) for item in items[: budget.max_list_items]]
        if len(items) > budget.max_list_items:
            bounded.append(
                {
                    "truncated": True,
                    "omitted_items": len(items) - budget.max_list_items,
                }
            )
        return bounded
    text = str(value)
    return _bounded_value(text, budget, depth=depth)


def _clip(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars)
    return text[:keep] + f"...[truncated {len(text) - keep} chars]"


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


__all__ = [
    "BackgroundContextBudget",
    "DEFAULT_BACKGROUND_CONTEXT_BUDGET",
    "bounded_background_context_payload",
]
