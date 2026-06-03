
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..artifacts.registry import latest_artifact_records
from ..runtime_errors import runtime_error_report
from ..settings.defaults import default_config_value
from .context_budget import (
    BackgroundContextPayloadRequest,
    background_context_budget_from_config,
    bounded_background_context_payload,
)
from .models import ConversationThread
from .runtime_tool_policy import (
    BackgroundToolPolicyRequest,
    background_allowed_tools,
    background_control_action_lines,
    background_tool_policy_decision,
)
from .runtime_utils import json_block, pending_wake_payload
from .store import ConversationStore


@dataclass(frozen=True)
class _BackgroundContextLoad:
    agent: object
    store: ConversationStore
    thread: ConversationThread
    config: object | None
    policy_request: BackgroundToolPolicyRequest
    load_errors: list[dict[str, Any]]


def context_markdown(*, agent: object, store: ConversationStore, thread: ConversationThread, request) -> str:
    policy_request = _tool_policy_request(agent, request)
    bounded = _bounded_context(agent, store, thread, policy_request)
    policy_decision = background_tool_policy_decision(getattr(agent, "config", None), request=policy_request)
    sections = [
        ("Active Wake Signal", request.wake_signal or {}),
        ("Conversation Thread", bounded["thread"]),
        ("Runtime Load Errors", bounded.get("load_errors") or []),
        ("Recent Messages", bounded["messages"]),
        ("Bound Tasks", bounded["tasks"]),
        ("Channel Bindings", bounded["channel_bindings"]),
        ("Recent Observations", bounded["observations"]),
        ("Pending Guidance", bounded["guidance"]),
        ("Pending Wake Signals", bounded["pending_wake_signals"]),
        ("Recovery Snapshot", bounded["recovery_snapshot"]),
        ("Agent Tree Snapshot", bounded["agent_tree"]),
        ("Control Action Policy", policy_decision.to_dict()),
    ]
    lines = _context_header(request, thread)
    for title, payload in sections:
        lines.extend(["", f"## {title}", json_block(payload)])
    lines.extend([
        "",
        "## Available Control Actions",
        *background_control_action_lines(getattr(agent, "config", None), request=policy_request),
        "[/background-main-agent-context]",
    ])
    return "\n".join(lines)


def _bounded_context(
    agent: object,
    store: ConversationStore,
    thread: ConversationThread,
    policy_request: BackgroundToolPolicyRequest,
) -> dict[str, Any]:
    config = getattr(agent, "config", None)
    load_errors: list[dict[str, Any]] = []
    state = _BackgroundContextLoad(agent, store, thread, config, policy_request, load_errors)
    visible_run_ids = _thread_active_task_ids(state)
    agent_tree = _agent_tree_payload(state, visible_run_ids)
    bundle = _context_bundle(state)
    pending_wake_signals = _pending_wake_signals(state)
    recovery_snapshot = _safe_recovery_snapshot(state, visible_run_ids)
    return bounded_background_context_payload(
        BackgroundContextPayloadRequest(
            bundle=bundle,
            pending_wake_signals=pending_wake_signals,
            agent_tree=agent_tree,
            recovery_snapshot=recovery_snapshot,
            load_errors=load_errors,
            budget=background_context_budget_from_config(config),
        )
    )


def _context_bundle(state: _BackgroundContextLoad) -> dict[str, Any]:
    try:
        if callable(getattr(state.store, "context_bundle_report", None)):
            bundle, load_errors = state.store.context_bundle_report(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
            )
            state.load_errors.extend(load_errors)
            return bundle
        return state.store.context_bundle(
            state.thread.thread_id,
            recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
        )
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.context_bundle"))
        return _minimal_context_bundle(state.thread)


def _pending_wake_signals(state: _BackgroundContextLoad) -> list[dict[str, Any]]:
    try:
        if callable(getattr(state.store, "pending_wake_signals_report", None)):
            signals, load_errors = state.store.pending_wake_signals_report(
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
            state.load_errors.extend(load_errors)
            return [item.to_dict() for item in signals if item.thread_id == state.thread.thread_id]
        return pending_wake_payload(
            state.store,
            state.thread.thread_id,
            limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
        )
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.pending_wake_signals"))
        return []


def _agent_tree_payload(
    state: _BackgroundContextLoad,
    visible_run_ids: list[str],
) -> dict[str, Any]:
    try:
        return agent_tree_status_payload(
            state.agent,
            {
                "visible_run_ids": visible_run_ids,
                "allowed_tools": background_allowed_tools(state.config, request=state.policy_request),
            },
        )
    except Exception as exc:
        report = runtime_error_report(exc, context="background_context.agent_tree")
        state.load_errors.append(report)
        return {
            "schema_version": "agent_tree_status.v1",
            "effect": "read_only",
            "nodes": [],
            "edges": [],
            "warnings": ["agent_tree_load_error"],
            "load_error": report,
        }


def _safe_recovery_snapshot(
    state: _BackgroundContextLoad,
    visible_run_ids: list[str],
) -> dict[str, Any]:
    try:
        return _recovery_snapshot(state.agent, state.store, state.thread.thread_id, visible_run_ids)
    except Exception as exc:
        report = runtime_error_report(exc, context="background_context.recovery_snapshot")
        state.load_errors.append(report)
        return {
            "schema_version": "background_recovery_snapshot.v1",
            "effect": "read_only",
            "does_not_block": True,
            "load_error": report,
        }


def _tool_policy_request(agent: object, request: object) -> BackgroundToolPolicyRequest:
    return BackgroundToolPolicyRequest(
        reason=str(getattr(request, "reason", "") or ""),
        wake_signal=getattr(request, "wake_signal", None),
        config=getattr(agent, "config", None),
        owner_policy=getattr(agent, "owner_policy", None),
        policy_snapshot=_policy_snapshot_from_request(request),
    )


def _policy_snapshot_from_request(request: object) -> dict[str, Any]:
    wake = getattr(request, "wake_signal", None)
    if isinstance(wake, dict) and isinstance(wake.get("policy_snapshot"), dict):
        return dict(wake["policy_snapshot"])
    return {}


def _config_int(config: object | None, key: str) -> int:
    if config is None:
        return max(0, int(default_config_value(key)))
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return max(0, int(default_config_value(key)))


def _context_header(request, thread: ConversationThread) -> list[str]:
    return [
        "[background-main-agent-context]",
        f"reason: {request.reason}",
        f"thread_id: {thread.thread_id}",
        f"task_id: {request.task_id or ''}",
    ]


def _thread_active_task_ids(state: _BackgroundContextLoad) -> list[str]:
    latest = _latest_thread_with_load_error(state)
    source = latest or state.thread
    return [str(item or "").strip() for item in source.active_task_ids if str(item or "").strip()]


def _latest_thread_with_load_error(state: _BackgroundContextLoad) -> ConversationThread | None:
    try:
        if not callable(getattr(state.store, "load_thread_report", None)):
            return state.store.load_thread(state.thread.thread_id)
        latest, load_error = state.store.load_thread_report(state.thread.thread_id)
        if load_error is not None:
            load_error["consumer_context"] = "background_context.thread_active_tasks"
            state.load_errors.append(load_error)
        return latest
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.thread_active_tasks"))
        return None


def _minimal_context_bundle(thread: ConversationThread) -> dict[str, Any]:
    return {
        "thread": thread.to_dict(),
        "messages": [],
        "tasks": [],
        "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
        "observations": [],
        "guidance": [],
    }


def _recovery_snapshot(agent: object, store: ConversationStore, thread_id: str, visible_run_ids: list[str]) -> dict[str, Any]:
    claim = store.load_background_run_claim(thread_id)
    previous = claim.get("previous_claim") if isinstance(claim.get("previous_claim"), dict) else {}
    tree = agent_tree_status_payload(agent, {"visible_run_ids": visible_run_ids})
    records = latest_artifact_records(getattr(agent, "root", "."))
    payload = {
        "schema_version": "background_recovery_snapshot.v1",
        "effect": "read_only",
        "does_not_block": True,
        "current_claim_status": str(claim.get("status") or ""),
        "current_claim_id": str(claim.get("claim_id") or ""),
        "current_claim_reason": str(claim.get("reason") or ""),
        "previous_claim_status": str(previous.get("status") or ""),
        "previous_claim_error": previous.get("last_error") if isinstance(previous.get("last_error"), dict) else {},
        "takeover": claim.get("takeover") if isinstance(claim.get("takeover"), dict) else {},
        "tree_status_buckets": tree.get("status_buckets") if isinstance(tree.get("status_buckets"), dict) else {},
        "artifact_registry_count": len(records),
        "artifact_registry_status_counts": _artifact_status_counts(records),
        "takeover_advice": "接手前先核对 claim、任务树和产物登记；不要把模型文本里的完成声明当成事实。",
    }
    if isinstance(claim.get("load_error"), dict):
        payload["claim_load_error"] = claim["load_error"]
    return payload


def _artifact_status_counts(records: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records.values():
        status = str(getattr(record, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts
