
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..memory_archive.tokens import estimate_tokens


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
    max_total_tokens: int = 8000


DEFAULT_BACKGROUND_CONTEXT_BUDGET = BackgroundContextBudget()


@dataclass(frozen=True)
class BackgroundContextPayloadRequest:
    bundle: dict[str, Any]
    active_wake_signal: dict[str, Any] | None
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
        max_total_tokens=_config_int(
            config,
            "background_context_max_total_tokens",
            defaults.max_total_tokens,
        ),
    )


def bounded_background_context_payload(request: BackgroundContextPayloadRequest) -> dict[str, Any]:
    limits = request.budget or DEFAULT_BACKGROUND_CONTEXT_BUDGET
    initial = _bounded_payload(request, limits)
    return _fit_total_budget(request, limits, initial)


def _bounded_payload(
    request: BackgroundContextPayloadRequest,
    limits: BackgroundContextBudget,
) -> dict[str, Any]:
    return {
        "thread": _bounded_value(request.bundle.get("thread"), limits),
        "active_wake_signal": _bounded_active_wake_signal(
            request.active_wake_signal or {},
            limits,
        ),
        "messages": _bounded_top_level_list(
            request.bundle.get("messages"),
            limits,
            render=_bounded_message,
            keep_tail=True,
        ),
        "tasks": _bounded_top_level_list(
            request.bundle.get("tasks"),
            limits,
            render=_bounded_value,
        ),
        "channel_bindings": _bounded_top_level_list(
            request.bundle.get("channel_bindings"),
            limits,
            render=_bounded_value,
        ),
        "observations": _bounded_top_level_list(
            request.bundle.get("observations"),
            limits,
            render=_bounded_observation,
            keep_tail=True,
        ),
        "guidance": _bounded_top_level_list(
            request.bundle.get("guidance"),
            limits,
            render=_bounded_observation,
            keep_tail=True,
        ),
        "pending_wake_signals": _bounded_top_level_list(
            request.pending_wake_signals,
            limits,
            render=_bounded_observation,
        ),
        "task_runtime_state": _bounded_task_runtime_state(
            request.task_runtime_state,
            limits,
        ),
        "recovery_snapshot": _bounded_value(request.recovery_snapshot or {}, limits),
        "agent_tree": _bounded_value(request.agent_tree, limits),
        "load_errors": _bounded_value(request.load_errors, limits),
    }


def _fit_total_budget(
    request: BackgroundContextPayloadRequest,
    limits: BackgroundContextBudget,
    initial: dict[str, Any],
) -> dict[str, Any]:
    """Fit the whole background projection while keeping durable state intact."""

    configured = int(limits.max_total_tokens or 0)
    if configured <= 0:
        return initial
    max_total_tokens = max(2048, configured)
    payload_limit = max(1024, max_total_tokens - 256)
    initial_tokens = estimate_tokens(initial)
    candidates = [limits, *(_scaled_budget(limits, divisor) for divisor in (2, 4, 8, 16))]
    candidates.append(
        BackgroundContextBudget(
            max_string_chars=64,
            max_list_items=1,
            max_dict_items=8,
            max_depth=2,
            max_total_tokens=max_total_tokens,
        )
    )
    seen: set[tuple[int, int, int, int]] = set()
    for pass_index, candidate in enumerate(candidates):
        signature = (
            candidate.max_string_chars,
            candidate.max_list_items,
            candidate.max_dict_items,
            candidate.max_depth,
        )
        if signature in seen:
            continue
        seen.add(signature)
        payload = initial if pass_index == 0 else _bounded_payload(request, candidate)
        if estimate_tokens(payload) <= payload_limit:
            return _with_projection_metadata(
                payload,
                max_total_tokens=max_total_tokens,
                initial_tokens=initial_tokens,
                pass_index=pass_index,
            )
    return _minimal_projection(
        request,
        max_total_tokens=max_total_tokens,
        initial_tokens=initial_tokens,
    )


def _scaled_budget(budget: BackgroundContextBudget, divisor: int) -> BackgroundContextBudget:
    return BackgroundContextBudget(
        max_string_chars=max(64, int(budget.max_string_chars or 0) // divisor),
        max_list_items=max(1, int(budget.max_list_items or 0) // divisor),
        max_dict_items=max(8, int(budget.max_dict_items or 0) // divisor),
        max_depth=max(2, int(budget.max_depth or 0) - divisor.bit_length() + 1),
        max_total_tokens=budget.max_total_tokens,
    )


def _with_projection_metadata(
    payload: dict[str, Any],
    *,
    max_total_tokens: int,
    initial_tokens: int,
    pass_index: int,
) -> dict[str, Any]:
    result = dict(payload)
    result["_projection"] = {
        "schema_version": 1,
        "max_total_tokens": max_total_tokens,
        "estimated_tokens_before": initial_tokens,
        "total_budget_applied": pass_index > 0,
        "shape_pass": pass_index,
        "durable_sources_unchanged": True,
    }
    return result


def _minimal_projection(
    request: BackgroundContextPayloadRequest,
    *,
    max_total_tokens: int,
    initial_tokens: int,
) -> dict[str, Any]:
    """Last structural fallback for an exceptionally dense background state."""

    tiny = BackgroundContextBudget(
        max_string_chars=24,
        max_list_items=1,
        max_dict_items=3,
        max_depth=1,
        max_total_tokens=max_total_tokens,
    )
    return _with_projection_metadata(
        _bounded_payload(request, tiny),
        max_total_tokens=max_total_tokens,
        initial_tokens=initial_tokens,
        pass_index=99,
    )


def _bounded_top_level_list(
    value: object,
    budget: BackgroundContextBudget,
    *,
    render,
    keep_tail: bool = False,
) -> list[Any]:
    items = _list(value)
    limit = max(0, int(budget.max_list_items or 0))
    if limit <= 0 or len(items) <= limit:
        selected = items
        omitted = 0
    elif keep_tail:
        selected = items[-limit:]
        omitted = len(items) - limit
    else:
        selected = items[:limit]
        omitted = len(items) - limit
    bounded = [render(item, budget) for item in selected]
    if omitted:
        marker = {
            "truncated": True,
            "omitted_items": omitted,
            "omitted_position": "head" if keep_tail else "tail",
        }
        return [marker, *bounded] if keep_tail else [*bounded, marker]
    return bounded


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


# LLM: The active wake is the current turn's typed input. Shape-pressure may
# shorten strings but must not drop its metadata, evidence refs, or batch members
# in favor of less authoritative bookkeeping fields.
# 函数用途: 有界投影当前唤醒事件，完整保留本批事件结构并按预算缩短各段正文。
def _bounded_active_wake_signal(
    value: object,
    budget: BackgroundContextBudget,
) -> dict[str, Any]:
    row = dict(value) if isinstance(value, dict) else {}
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    events = metadata.get("events") if isinstance(metadata.get("events"), list) else []
    list_floor = max(
        len(events),
        len(row.get("evidence_refs")) if isinstance(row.get("evidence_refs"), list) else 0,
        1,
    )
    active_budget = BackgroundContextBudget(
        max_string_chars=max(1, int(budget.max_string_chars or 0)),
        max_list_items=max(list_floor, int(budget.max_list_items or 0)),
        max_dict_items=max(32, int(budget.max_dict_items or 0)),
        max_depth=max(8, int(budget.max_depth or 0)),
        max_total_tokens=budget.max_total_tokens,
    )
    ordered_keys = (
        "wake_signal_id",
        "thread_id",
        "reason",
        "root_task_id",
        "source_agent_id",
        "parent_agent_id",
        "summary",
        "evidence_refs",
        "metadata",
        "urgency",
        "severity",
        "created_at",
        "handled_at",
        "status",
        "dedupe_key",
    )
    ordered = {key: row[key] for key in ordered_keys if key in row}
    ordered.update({key: item for key, item in row.items() if key not in ordered})
    return _bounded_observation(ordered, active_budget)


def _bounded_task_runtime_state(
    value: object,
    budget: BackgroundContextBudget,
) -> Any:
    """Keep current typed task facts ahead of historical prose under pressure.

    A generic dictionary cap used to keep the first few bookkeeping fields and
    silently drop the later ``audit_sources`` / ``audit_summary`` fields.  The
    model then saw an old assistant or child summary but not the current source
    ledger, which could make a final report rename a source or misstate totals.

    会话运行时 turn context gives current runtime/tool facts higher authority
    than earlier model prose.  This projection preserves that same ordering
    without inventing business rules: Audit rows remain a bounded, read-only
    view of the exact persisted source and receipt facts.
    """
    row = dict(value) if isinstance(value, dict) else None
    if not isinstance(row, dict) or str(row.get("work_kind") or "").lower() != "audit":
        return _bounded_value(value, budget)

    # Keep enough room for a useful cross-source snapshot even when the whole
    # background context has entered its smallest shape pass.  More sources
    # remain in the durable watch registry and the truncation marker tells the
    # model to inspect them through the current task-scoped tool surface.
    source_limit = max(3, min(16, max(0, int(budget.max_list_items or 0))))
    fact_budget = BackgroundContextBudget(
        max_string_chars=max(128, min(512, int(budget.max_string_chars or 0))),
        max_list_items=source_limit,
        max_dict_items=max(16, min(48, int(budget.max_dict_items or 0))),
        max_depth=max(6, int(budget.max_depth or 0)),
        max_total_tokens=budget.max_total_tokens,
    )
    projected: dict[str, Any] = {}
    for key in (
        "schema_version",
        "task_id",
        "status",
        "work_kind",
        "work_name",
    ):
        if key in row:
            projected[key] = _bounded_value(row[key], fact_budget)

    # Aggregate coverage comes before per-source rows so an exceptionally large
    # source set still exposes exact task totals even when the list is clipped.
    if "audit_summary" in row:
        projected["audit_summary"] = _bounded_audit_summary(
            row.get("audit_summary"),
            fact_budget,
        )
    if "audit_sources" in row:
        projected["audit_sources"] = _bounded_top_level_list(
            row.get("audit_sources"),
            fact_budget,
            render=_bounded_audit_source,
        )
    if "task_progress" in row:
        projected["task_progress"] = _bounded_value(
            row.get("task_progress"),
            fact_budget,
        )
    for key in (
        "goal",
        "created_at",
        "duration_seconds",
        "expires_at",
        "cancellation_scope",
        "task_path",
    ):
        if key in row:
            projected[key] = _bounded_value(row[key], fact_budget)
    return projected


def _bounded_audit_summary(value: object, budget: BackgroundContextBudget) -> Any:
    row = dict(value) if isinstance(value, dict) else None
    if not isinstance(row, dict):
        return _bounded_value(value, budget)
    # ``source_urls`` duplicates the exact per-source rows and can dominate a
    # pressured prompt.  All other persisted aggregate facts stay available.
    return _bounded_value(
        {key: item for key, item in row.items() if key != "source_urls"},
        budget,
    )


def _bounded_audit_source(value: object, budget: BackgroundContextBudget) -> Any:
    row = dict(value) if isinstance(value, dict) else None
    if not isinstance(row, dict):
        return _bounded_value(value, budget)
    allowed = (
        "watch_id",
        "source_id",
        "source_url",
        "cursor",
        "closed",
        "window_complete",
        "collection_active",
        "watch_window_seconds",
        "elapsed_seconds",
        "remaining_seconds",
        "state_available",
        "audit_receipt",
        "last_error_code",
    )
    return _bounded_value(
        {key: row[key] for key in allowed if key in row},
        budget,
    )


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
