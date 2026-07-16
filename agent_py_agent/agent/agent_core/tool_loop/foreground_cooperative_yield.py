from __future__ import annotations

"""Cooperatively hand a durable interactive task to the background runtime.

The decision is deliberately structural: request source, exact conversation-task
binding, and a configured tool-round quantum.  User wording never grants runtime
authority here.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_RUNTIME_REASON = "foreground_cooperative_yield"
_REQUEST_ATTR = "foreground_cooperative_yield_request_id"
_FALLBACK_POLICY_ATTR = "foreground_cooperative_yield_fallback_policy_id"
_ACTIVE_POLICY_ATTR = "foreground_cooperative_yield_policy_id"
_INTERACTIVE_SOURCES = frozenset({"chat", "gateway"})
_RECENT_COMMITTED_GUIDANCE_LIMIT = 8
_TASK_CONTEXT_TEXT_LIMIT = 1200


@dataclass(frozen=True)
class _TaskBinding:
    attrs: dict[str, Any]
    store: Any
    request_id: str
    thread_id: str
    task_id: str


@dataclass(frozen=True)
class _PolicySpec:
    kind: str
    config_key: str
    default_seconds: int
    minimum_seconds: int
    reason: str


def maybe_queue_foreground_cooperative_yield(
    agent: object,
    params: object,
    *,
    tool_rounds: int,
) -> bool:
    """Queue one model-authored interim reply and a crash-safe background handoff."""
    if not _eligible(agent, params, tool_rounds=tool_rounds):
        return False
    binding = _task_binding(agent, params)
    if binding is None or not _active_task_link(binding.store, binding.thread_id, binding.task_id):
        return False
    try:
        fallback = _set_policy(
            agent,
            binding,
            _PolicySpec(
                kind="foreground_task_handoff_fallback",
                config_key="foreground_task_handoff_fallback_seconds",
                default_seconds=300,
                minimum_seconds=30,
                reason="前台运行已让出；若正常交接未完成，由耐久提醒恢复同一任务。",
            ),
        )
    except Exception:
        return False
    _record_fallback(params, binding, fallback, tool_rounds=tool_rounds)
    _queue_interim_reply(agent, params)
    return True


def is_foreground_cooperative_yield_response(params: object, response: object) -> bool:
    """Return whether this exact request produced the registered handoff reply."""
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return False
    request_id = str(getattr(params, "request_id", "") or "").strip()
    return bool(
        request_id
        and str(attrs.get(_REQUEST_ATTR) or "").strip() == request_id
        and str(getattr(response, "runtime_reason", "") or "").strip() == _RUNTIME_REASON
        and str(getattr(params, "source", "") or "").strip() in _INTERACTIVE_SOURCES
    )


def finalize_foreground_cooperative_yield(agent: object, ctx: object) -> dict[str, Any]:
    """Activate the fast continuation only after foreground model/tool work has ended.

    A slow fallback policy is written before the interim reply.  Normal finalization
    replaces it with the short-delay policy; a crash therefore delays recovery but
    cannot lose the durable task.
    """
    binding = _task_binding(agent, ctx)
    if binding is None:
        return {"activated": False, "reason": "no_task_attributes"}
    if str(binding.attrs.get(_REQUEST_ATTR) or "").strip() != binding.request_id:
        return {"activated": False, "reason": "not_registered"}
    fallback_id = str(binding.attrs.get(_FALLBACK_POLICY_ATTR) or "").strip()
    if not is_foreground_cooperative_yield_response(ctx, getattr(ctx, "final_response", None)):
        _disable_policy(binding.store, fallback_id)
        return {"activated": False, "reason": "reply_not_committed"}
    if not _active_task_link(binding.store, binding.thread_id, binding.task_id):
        _disable_policy(binding.store, fallback_id)
        return {"activated": False, "reason": "task_not_active"}
    try:
        policy = _set_policy(
            agent,
            binding,
            _PolicySpec(
                kind="foreground_task_continuation",
                config_key="foreground_task_resume_delay_seconds",
                default_seconds=5,
                minimum_seconds=1,
                reason="继续前台已让出的同一持久任务；读取现有工作区和进度后从安全点续作。",
            ),
        )
    except Exception:
        # Keep the already-durable slow fallback enabled.
        return {
            "activated": False,
            "reason": "fast_policy_write_failed",
            "fallback_policy_id": fallback_id,
        }
    _disable_policy(binding.store, fallback_id)
    policy_id = str(getattr(policy, "policy_id", "") or "")
    binding.attrs[_ACTIVE_POLICY_ATTR] = policy_id
    return {
        "activated": True,
        "policy_id": policy_id,
        "fallback_policy_id": fallback_id,
        "thread_id": binding.thread_id,
        "task_id": binding.task_id,
    }


def _task_binding(agent: object, params: object) -> _TaskBinding | None:
    attrs = getattr(params, "task_attributes", None)
    store = getattr(agent, "conversation_store", None)
    request_id = str(getattr(params, "request_id", "") or "").strip()
    if not isinstance(attrs, dict) or store is None or not request_id:
        return None
    return _TaskBinding(
        attrs=attrs,
        store=store,
        request_id=request_id,
        thread_id=str(attrs.get("conversation_thread_id") or "").strip(),
        task_id=str(attrs.get("conversation_task_id") or "").strip(),
    )


def _set_policy(
    agent: object,
    binding: _TaskBinding,
    spec: _PolicySpec,
) -> object:
    if existing := _enabled_policy(binding, spec.kind):
        return existing
    return binding.store.set_progress_policy(
        {
            "thread_id": binding.thread_id,
            "task_id": binding.task_id,
            "interval_seconds": _config_int(
                agent,
                spec.config_key,
                spec.default_seconds,
                minimum=spec.minimum_seconds,
            ),
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": spec.kind,
                "tool": "runtime_cooperative_yield",
                "request_id": binding.request_id,
                "task_id": binding.task_id,
                "reason": spec.reason,
            },
        }
    )


def _enabled_policy(binding: _TaskBinding, kind: str) -> object | None:
    policies, load_errors = binding.store.list_progress_policies_report(enabled_only=True)
    if load_errors:
        raise RuntimeError("foreground continuation policy state is unavailable")
    return next(
        (
            item
            for item in policies
            if str(getattr(item, "thread_id", "") or "") == binding.thread_id
            and str(getattr(item, "task_id", "") or "") == binding.task_id
            and str((getattr(item, "metadata", {}) or {}).get("kind") or "") == kind
        ),
        None,
    )


def _record_fallback(params: object, binding: _TaskBinding, fallback: object, *, tool_rounds: int) -> None:
    binding.attrs[_REQUEST_ATTR] = binding.request_id
    fallback_id = str(getattr(fallback, "policy_id", "") or "")
    binding.attrs[_FALLBACK_POLICY_ATTR] = fallback_id
    state = getattr(params, "live_archive_state", None)
    if isinstance(state, dict):
        state[_RUNTIME_REASON] = {
            "request_id": binding.request_id,
            "thread_id": binding.thread_id,
            "task_id": binding.task_id,
            "tool_rounds": max(0, int(tool_rounds)),
            "fallback_policy_id": fallback_id,
        }


def _queue_interim_reply(agent: object, params: object) -> None:
    from .natural_user_reply import queue_natural_user_reply

    facts: dict[str, object] = {
        "reply_is_interim": True,
        "task_continues_in_background": True,
        "user_can_continue_conversation": True,
        "further_runtime_action_required": True,
        "allow_time_estimate": False,
    }
    facts.update(_progress_reply_facts(agent, params))
    queue_natural_user_reply(
        params,
        kind=_RUNTIME_REASON,
        facts=facts,
    )


def _progress_reply_facts(agent: object, params: object) -> dict[str, object]:
    """Project current durable progress into the model-authored handoff reply."""
    from ...task_progress import read_task_progress, task_progress_summary
    from ..runtime.owner_roots import runtime_owner_root
    from ..runtime.task_identity import durable_task_id

    task_id = durable_task_id(params)
    if not task_id:
        return {}
    summary = task_progress_summary(read_task_progress(runtime_owner_root(agent), task_id))
    records = [item for item in list(getattr(params, "archive_tool_calls", None) or []) if isinstance(item, dict)]
    successful_records = [item for item in records if item.get("ok") is True]
    facts: dict[str, object] = {
        "successful_actions_this_turn": len(successful_records),
        "failed_actions_this_turn": sum(1 for item in records if item.get("ok") is False),
    }
    facts.update(_durable_task_context_facts(agent, task_id))
    successful_progress_actions = _successful_progress_actions(records)
    if "select" in successful_progress_actions:
        facts["task_workspace_selected_this_turn"] = True
    if any(
        (tool_name := str(item.get("tool") or "").strip())
        and tool_name != "task_progress"
        for item in successful_records
    ):
        facts["runtime_access_confirmed"] = True
    current_request = str(
        getattr(params, "root_user_prompt", "")
        or getattr(params, "user_prompt", "")
        or ""
    ).strip()
    if current_request:
        facts["current_user_request"] = current_request
    progress_is_current = _progress_snapshot_is_current_turn(records)
    if progress_is_current:
        facts["open_progress_items"] = (
            sum(
                int(summary.get("counts", {}).get(status) or 0)
                for status in ("pending", "in_progress", "blocked", "unknown")
            )
            if isinstance(summary.get("counts"), dict)
            else 0
        )
    if progress_is_current and str(summary.get("summary") or "").strip():
        facts["current_progress"] = str(summary["summary"])
    if progress_is_current and str(summary.get("next_action") or "").strip():
        facts["next_action"] = str(summary["next_action"])
    if not progress_is_current:
        facts["existing_task_selected_this_turn"] = True
    return facts


def _durable_task_context_facts(agent: object, task_id: str) -> dict[str, object]:
    """Return bounded context for this exact owner-scoped durable task.

    This is a presentation aid, not a task selector. Identity comes only from
    the current run's durable task id and the owner-scoped conversation store;
    pending guidance and another task's state are never projected.
    """
    store = getattr(agent, "conversation_store", None)
    if store is None or not task_id:
        return {}
    try:
        thread, thread_error = store.thread_for_task_report(task_id)
        if thread_error is not None or thread is None:
            return {}
        links, link_errors = store.task_links_report(thread.thread_id)
        if link_errors:
            return {}
        link = next(
            (
                item
                for item in links
                if str(getattr(item, "task_id", "") or "").strip() == task_id
            ),
            None,
        )
        if link is None:
            return {}
        delivered, guidance_errors = store.committed_guidance_report(
            (("request", task_id), ("task", task_id)),
            per_target_limit=0,
        )
        if guidance_errors:
            return {}
    except Exception:
        return {}

    facts: dict[str, object] = {"prior_task_context_available": True}
    goal = _bounded_task_context_text(getattr(link, "goal", ""))
    if goal:
        facts["current_task_goal"] = goal
    task_path = str(getattr(link, "task_path", "") or "").strip()
    if task_path:
        facts["current_task_workspace_name"] = Path(task_path).name
    facts["committed_task_guidance_count"] = len(delivered)
    recent_messages = [
        message
        for item in delivered[-_RECENT_COMMITTED_GUIDANCE_LIMIT:]
        if (message := _bounded_task_context_text(getattr(item, "message", "")))
    ]
    if recent_messages:
        facts["recent_committed_task_guidance"] = recent_messages
    return facts


def _bounded_task_context_text(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= _TASK_CONTEXT_TEXT_LIMIT:
        return text
    return text[: _TASK_CONTEXT_TEXT_LIMIT - 1].rstrip() + "…"


def _progress_snapshot_is_current_turn(records: list[dict[str, object]]) -> bool:
    """Do not present an old task snapshot as progress on a new follow-up turn."""
    actions = _successful_progress_actions(records)
    if "select" not in actions:
        return True
    if actions.intersection({"start", "update"}):
        return True
    return any(
        str(item.get("tool") or "").strip()
        in {"create_subagents", "schedule_child_subagents"}
        and item.get("ok") is True
        for item in records
    )


def _successful_progress_actions(records: list[dict[str, object]]) -> set[str]:
    """Return only progress transitions that the structured tool ledger accepted."""
    actions: set[str] = set()
    for item in records:
        if item.get("ok") is not True or str(item.get("tool") or "").strip() != "task_progress":
            continue
        parameters = item.get("parameters")
        if isinstance(parameters, dict):
            actions.add(str(parameters.get("action") or "").strip().lower())
    return actions


def _eligible(agent: object, params: object, *, tool_rounds: int) -> bool:
    if str(getattr(params, "source", "") or "").strip() not in _INTERACTIVE_SOURCES:
        return False
    quantum = _config_int(
        agent,
        "foreground_task_tool_round_quantum",
        4,
        minimum=0,
    )
    if quantum <= 0 or int(tool_rounds) < quantum:
        return False
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return False
    if str(attrs.get("conversation_lane") or "").strip().lower() != "task":
        return False
    request_id = str(getattr(params, "request_id", "") or "").strip()
    return bool(
        request_id
        and str(attrs.get("conversation_thread_id") or "").strip()
        and str(attrs.get("conversation_task_id") or "").strip()
        and str(attrs.get(_REQUEST_ATTR) or "").strip() != request_id
    )


def _active_task_link(store: object, thread_id: str, task_id: str) -> bool:
    if not thread_id or not task_id:
        return False
    try:
        links, load_errors = store.active_task_links_report(thread_id)
    except Exception:
        return False
    if load_errors:
        return False
    return any(
        str(getattr(item, "task_id", "") or "").strip() == task_id
        and str(getattr(item, "status", "") or "").strip().lower() == "active"
        for item in links
    )


def _disable_policy(store: object, policy_id: str) -> None:
    if not policy_id:
        return
    try:
        store.disable_progress_policy(policy_id)
    except Exception:
        pass


def _config_int(agent: object, key: str, default: int, *, minimum: int) -> int:
    value = getattr(getattr(agent, "config", None), key, default)
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return max(minimum, int(default))


__all__ = [
    "finalize_foreground_cooperative_yield",
    "is_foreground_cooperative_yield_response",
    "maybe_queue_foreground_cooperative_yield",
]
