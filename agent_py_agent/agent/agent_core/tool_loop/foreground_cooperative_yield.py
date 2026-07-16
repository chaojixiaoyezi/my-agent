from __future__ import annotations

"""Cooperatively hand a durable interactive task to the background runtime.

The decision is deliberately structural: request source, exact conversation-task
binding, and a configured tool-round quantum.  User wording never grants runtime
authority here.
"""

from dataclasses import dataclass
from typing import Any

_RUNTIME_REASON = "foreground_cooperative_yield"
_REQUEST_ATTR = "foreground_cooperative_yield_request_id"
_FALLBACK_POLICY_ATTR = "foreground_cooperative_yield_fallback_policy_id"
_ACTIVE_POLICY_ATTR = "foreground_cooperative_yield_policy_id"
_INTERACTIVE_SOURCES = frozenset({"chat", "gateway"})


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
    _queue_interim_reply(params, tool_rounds=tool_rounds)
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


def _queue_interim_reply(params: object, *, tool_rounds: int) -> None:
    from .natural_user_reply import queue_natural_user_reply

    queue_natural_user_reply(
        params,
        kind=_RUNTIME_REASON,
        facts={
            "reply_is_interim": True,
            "task_continues_in_background": True,
            "completed_foreground_tool_rounds": max(0, int(tool_rounds)),
            "user_can_continue_conversation": True,
            "further_runtime_action_required": True,
            "allow_time_estimate": False,
        },
    )


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
