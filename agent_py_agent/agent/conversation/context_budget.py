
from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class BackgroundContextPayloadRequest:
    bundle: dict[str, Any]
    pending_wake_signals: list[dict[str, Any]]
    agent_tree: dict[str, Any]
    task_runtime_state: dict[str, Any] = field(default_factory=dict)
    recovery_snapshot: dict[str, Any] | None = None
    load_errors: list[dict[str, Any]] = field(default_factory=list)
    budget: BackgroundContextBudget | None = None


def background_context_budget_from_config(config: object | None) -> BackgroundContextBudget:
    defaults = DEFAULT_BACKGROUND_CONTEXT_BUDGET
    if config is None:
        return defaults
    return BackgroundContextBudget(
        max_string_chars=_config_int(config, "background_context_max_string_chars", defaults.max_string_chars),
        max_list_items=_config_int(config, "background_context_max_list_items", defaults.max_list_items),
        max_dict_items=_config_int(config, "background_context_max_dict_items", defaults.max_dict_items),
        max_depth=_config_int(config, "background_context_max_depth", defaults.max_depth),
    )


def bounded_background_context_payload(request: BackgroundContextPayloadRequest) -> dict[str, Any]:
    limits = request.budget or DEFAULT_BACKGROUND_CONTEXT_BUDGET
    return {
        "thread": _bounded_value(request.bundle.get("thread"), limits),
        "messages": [_bounded_message(item, limits) for item in _list(request.bundle.get("messages"))],
        "tasks": [_bounded_value(item, limits) for item in _list(request.bundle.get("tasks"))],
        "channel_bindings": [
            _bounded_value(item, limits) for item in _list(request.bundle.get("channel_bindings"))
        ],
        "observations": [
            _bounded_observation(item, limits) for item in _list(request.bundle.get("observations"))
        ],
        "guidance": [
            _bounded_observation(item, limits) for item in _list(request.bundle.get("guidance"))
        ],
        "pending_wake_signals": [
            _bounded_observation(item, limits) for item in _list(request.pending_wake_signals)
        ],
        "task_runtime_state": _bounded_value(request.task_runtime_state, limits),
        "recovery_snapshot": _bounded_value(request.recovery_snapshot or {}, limits),
        "agent_tree": _bounded_value(request.agent_tree, limits),
        "load_errors": _bounded_value(request.load_errors, limits),
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


def _config_int(config: object, key: str, default: int) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return default


__all__ = [
    "BackgroundContextBudget",
    "BackgroundContextPayloadRequest",
    "DEFAULT_BACKGROUND_CONTEXT_BUDGET",
    "background_context_budget_from_config",
    "bounded_background_context_payload",
]
