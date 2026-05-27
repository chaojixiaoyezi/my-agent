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


# LLM: background_context_budget_from_config keeps background prompt clipping tied to AgentConfig.
# 函数用途: 从主配置读取后台上下文裁剪预算；没有配置对象时使用 schema 默认预算。
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


# LLM: _bounded_message clips a single conversation message for prompt injection.
# 函数用途: 裁剪消息 content 副本并保留原始长度标记，不修改会话账本。
def _bounded_message(value: object, budget: BackgroundContextBudget) -> dict[str, Any]:
    row = dict(value) if isinstance(value, dict) else {}
    content = str(row.get("content") or "")
    preview = _clip(content, budget.max_string_chars)
    row["content"] = preview
    if preview != content:
        row["content_truncated"] = True
        row["content_original_chars"] = len(content)
    return _bounded_value(row, budget)


# LLM: _bounded_observation clips observation text fields before background wake prompts.
# 函数用途: 裁剪 observation 的 summary/reason/content 副本，避免后台 prompt 膨胀。
def _bounded_observation(value: object, budget: BackgroundContextBudget) -> dict[str, Any]:
    row = dict(value) if isinstance(value, dict) else {}
    for key in ("summary", "reason", "content"):
        _clip_observation_field(row, key, budget.max_string_chars)
    return _bounded_value(row, budget)


# LLM: _clip_observation_field annotates one clipped observation field.
# 函数用途: 对单个 observation 字段写 preview、truncated 和 original_chars。
def _clip_observation_field(row: dict[str, Any], key: str, limit: int) -> None:
    if key not in row:
        return
    text = str(row.get(key) or "")
    preview = _clip(text, limit)
    row[key] = preview
    if preview != text:
        row[f"{key}_truncated"] = True
        row[f"{key}_original_chars"] = len(text)


# LLM: _bounded_value recursively bounds JSON-like context payloads.
# 函数用途: 按字符串长度、列表数量、字典数量和深度裁剪 prompt 副本。
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


# LLM: _clip produces a short string preview without touching durable content.
# 函数用途: 根据最大字符数截断字符串；0 或负数表示不截断。
def _clip(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars)
    return text[:keep] + f"...[truncated {len(text) - keep} chars]"


# LLM: _list normalizes optional list-like payloads for bounded context rendering.
# 函数用途: 非 list 值按空列表处理，避免坏账本字段打断后台 prompt 构造。
def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


# LLM: _config_int normalizes one background context budget field.
# 函数用途: 从配置对象读取非负整数，非法值回退到调用方给出的默认值。
def _config_int(config: object, key: str, fallback: int) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return fallback


__all__ = [
    "BackgroundContextBudget",
    "DEFAULT_BACKGROUND_CONTEXT_BUDGET",
    "background_context_budget_from_config",
    "bounded_background_context_payload",
]
