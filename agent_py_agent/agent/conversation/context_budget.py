# LLM: 后台上下文预算只裁展示副本；身份、可恢复引用及宿主窗口事实沿中性完成合同保留。
# 模块用途: 控制后台模型输入的大小，不修改持久账本、重新计时或以摘要裁决任务状态。
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts.subagent_completion import completion_service_window_facts
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
    subagent_completions: dict[str, object] | None = None
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


# LLM: rendered_keys 是渲染方按结构化开关（include_recent_messages、narrow_audit_event）算出的将渲染键集合；
# 只对这些键做有界投影和总预算估算，不按字段是否为空推断。None 保持原行为：全部键都参与。
# 函数用途: 生成后台上下文的有界展示副本，并按总预算收缩；不会渲染的节不占预算。
def bounded_background_context_payload(
    request: BackgroundContextPayloadRequest,
    *,
    rendered_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    return _fit_total_budget(request, request.budget or DEFAULT_BACKGROUND_CONTEXT_BUDGET, rendered_keys)


# LLM: 唯一的"payload 键 → 有界投影"表；新增后台上下文节时须同时登记到这里和渲染方的节表，合同测试核对两边一致。
# 函数用途: 只为将渲染的键生成有界投影，其余键不计算、不进入预算。
def _bounded_payload(
    request: BackgroundContextPayloadRequest,
    limits: BackgroundContextBudget,
    rendered_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    builders = {
        "thread": lambda: _bounded_value(request.bundle.get("thread"), limits),
        "active_wake_signal": lambda: _bounded_active_wake_signal(request.active_wake_signal or {}, limits),
        "subagent_completions": lambda: _bounded_subagent_completions(request.subagent_completions or {}, limits),
        "messages": lambda: _bounded_top_level_list(
            request.bundle.get("messages"), limits, render=_bounded_message, keep_tail=True),
        "tasks": lambda: _bounded_top_level_list(request.bundle.get("tasks"), limits, render=_bounded_value),
        "channel_bindings": lambda: _bounded_top_level_list(
            request.bundle.get("channel_bindings"), limits, render=_bounded_value),
        "observations": lambda: _bounded_top_level_list(
            request.bundle.get("observations"), limits, render=_bounded_observation, keep_tail=True),
        "guidance": lambda: _bounded_top_level_list(
            request.bundle.get("guidance"), limits, render=_bounded_observation, keep_tail=True),
        "pending_wake_signals": lambda: _bounded_top_level_list(
            request.pending_wake_signals, limits, render=_bounded_observation),
        "task_runtime_state": lambda: _bounded_task_runtime_state(request.task_runtime_state, limits),
        "recovery_snapshot": lambda: _bounded_value(request.recovery_snapshot or {}, limits),
        "agent_tree": lambda: _bounded_value(request.agent_tree, limits),
        "load_errors": lambda: _bounded_value(request.load_errors, limits),
    }
    return {key: build() for key, build in builders.items() if rendered_keys is None or key in rendered_keys}


# LLM: 总预算只估算将渲染的键；逐级缩小各节上限，仍超预算时退到最小结构投影。不改持久账。
# 函数用途: 把后台展示副本压进总 token 预算，并记录投影元数据。
def _fit_total_budget(request: BackgroundContextPayloadRequest, limits: BackgroundContextBudget,
                      rendered_keys: frozenset[str] | None) -> dict[str, Any]:
    """Fit the whole background projection while keeping durable state intact."""

    initial = _bounded_payload(request, limits, rendered_keys)
    configured = int(limits.max_total_tokens or 0)
    if configured <= 0:
        return initial
    max_total_tokens = max(2048, configured)
    payload_limit = max(1024, max_total_tokens - 256)
    initial_tokens = estimate_tokens(initial)
    candidates = [limits, *(_scaled_budget(limits, divisor) for divisor in (2, 4, 8, 16))]
    candidates.append(BackgroundContextBudget(max_string_chars=64, max_list_items=1, max_dict_items=8, max_depth=2,
                                              max_total_tokens=max_total_tokens))
    seen: set[tuple[int, int, int, int]] = set()
    for pass_index, candidate in enumerate(candidates):
        signature = (candidate.max_string_chars, candidate.max_list_items, candidate.max_dict_items, candidate.max_depth)
        if signature in seen:
            continue
        seen.add(signature)
        payload = initial if pass_index == 0 else _bounded_payload(request, candidate, rendered_keys)
        if estimate_tokens(payload) <= payload_limit:
            return _with_projection_metadata(
                payload,
                max_total_tokens=max_total_tokens,
                initial_tokens=initial_tokens,
                pass_index=pass_index,
            )
    return _minimal_projection(request, max_total_tokens=max_total_tokens, initial_tokens=initial_tokens,
                               rendered_keys=rendered_keys)


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


# LLM: 极端密集状态的最后结构兜底；同样只投影将渲染的键。
# 函数用途: 用最小上限生成后台展示副本。
def _minimal_projection(
    request: BackgroundContextPayloadRequest,
    *,
    max_total_tokens: int,
    initial_tokens: int,
    rendered_keys: frozenset[str] | None = None,
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
        _bounded_payload(request, tiny, rendered_keys),
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


# LLM: Child result refs are durable continuation inputs, like 会话运行时 inter-agent completion
# messages. Total-context pressure may shorten prose or reduce secondary ref lists, but it must
# keep every already-selected child identity and its exact canonical final_report_ref.
# 函数用途: 在后台提示预算收缩时保护直属子代理交接清单，避免后续定时轮只见终态却丢失报告入口。
def _bounded_subagent_completions(
    value: object,
    budget: BackgroundContextBudget,
) -> dict[str, Any]:
    row = dict(value) if isinstance(value, dict) else {}
    items = [item for item in _list(row.get("items")) if isinstance(item, dict)]
    if not items:
        return {}
    ref_limit = max(1, min(20, int(budget.max_list_items or 0)))
    bounded_items = [
        _bounded_subagent_completion_item(item, budget, ref_limit=ref_limit)
        for item in items
    ]
    return {
        "schema": str(row.get("schema") or ""),
        "workspace_task_id": str(row.get("workspace_task_id") or ""),
        "completion_root_task_ids": [
            str(item)
            for item in _list(row.get("completion_root_task_ids"))[:ref_limit]
            if str(item or "").strip()
        ],
        "total": _safe_nonnegative_int(row.get("total")),
        "visible_count": len(bounded_items),
        "omitted_count": _safe_nonnegative_int(row.get("omitted_count")),
        "items": bounded_items,
    }


# LLM: 完成清单显式选择公开字段；窗口事实沿共用合同传递，私有 runner 数据不进入模型。
# 函数用途: 缩短完成正文，同时保留状态、身份、冻结窗口事实和可读的完整报告引用。
def _bounded_subagent_completion_item(
    value: dict[str, Any],
    budget: BackgroundContextBudget,
    *,
    ref_limit: int,
) -> dict[str, Any]:
    message = str(value.get("completion_message") or "")
    preview = _clip(message, max(24, int(budget.max_string_chars or 0)))
    item: dict[str, Any] = {
        "task_id": str(value.get("task_id") or ""),
        "root_task_id": str(value.get("root_task_id") or ""),
        "status": str(value.get("status") or ""),
        "turn_end_reason": str(value.get("turn_end_reason") or ""),
        "failure_type": str(value.get("failure_type") or ""),
        "completion_schema_version": str(
            value.get("completion_schema_version") or ""
        ),
        "completion_message": preview,
        "final_report_ref": str(value.get("final_report_ref") or ""),
        "declared_output_refs": _exact_ref_slice(
            value.get("declared_output_refs"),
            limit=ref_limit,
        ),
        "artifact_refs": _exact_ref_slice(
            value.get("artifact_refs"),
            limit=ref_limit,
        ),
        "observed_at": value.get("observed_at", 0.0),
        **completion_service_window_facts(value),
    }
    if preview != message:
        item["completion_message_truncated"] = True
        item["completion_message_original_chars"] = len(message)
    elif value.get("completion_message_truncated") is True:
        item["completion_message_truncated"] = True
        item["completion_message_original_tokens"] = _safe_nonnegative_int(
            value.get("completion_message_original_tokens")
        )
    return item


# LLM: Exact ref strings are machine-usable inputs. Limit only their count under prompt pressure;
# clipping individual paths would turn an authoritative reference into an invalid guess.
# 函数用途: 保留少量完整路径或 URI，并去重空值。
def _exact_ref_slice(value: object, *, limit: int) -> list[str]:
    refs: list[str] = []
    for item in _list(value):
        ref = str(item or "").strip()
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= max(1, int(limit or 0)):
            break
    return refs


# LLM: Budget metadata is untrusted persisted input; normalize counts without letting malformed
# values abort context construction or become negative omission facts.
# 函数用途: 将上下文投影里的计数安全转换为非负整数。
def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
