from __future__ import annotations

from ..contracts.protocol_status import COMPACT_STATUS_READY_AFTER_ACTION_GUARD
from ..memory_archive import run_memory_compact_auto_cycle
from ..memory_archive.compact import MemoryCompactPlanOptions
from ..memory_archive.compact_auto import MemoryCompactAutoCycleOptions
from ._runtime_params import FinalizeContext
from .runtime.context_compactor import runtime_compact_policy
from .runtime.owner_roots import runtime_owner_root

_CONTEXT_OVERFLOW_REASONS = {
    "blackbox_output_overflow",
    "context_length_exceeded",
    "context_overflow",
    "maximum_context_length",
    "tool_output_context_overflow",
}
_DELIVERY_COMPLETE_MARKER = "[MAIN_AGENT_DELIVERY_COMPLETE]"
_MAX_CONSECUTIVE_NO_TOOL_PREFLIGHT_CONTINUATIONS = 3


def compact_auto_cycle_fields(agent, ctx: FinalizeContext, token_ledger: dict[str, int], *, request_id: str = "") -> dict:
    if _delivery_complete(ctx.final_response):
        return _compact_auto_delivery_complete_fields()
    trigger = _compact_trigger_from_runtime(ctx)
    if _should_return_after_continuation(ctx, trigger):
        return _compact_auto_continuation_return_fields()
    policy = runtime_compact_policy(agent, save=ctx.do_save)
    cycle = run_memory_compact_auto_cycle(
        runtime_owner_root(agent),
        MemoryCompactAutoCycleOptions(
            current_tokens=int(token_ledger.get("active", token_ledger["turn"])),
            max_context_tokens=policy.context_window_tokens,
            trigger_percent=policy.trigger_percent,
            plan_options=MemoryCompactPlanOptions(
                session_id=getattr(agent, "session_id", agent.config.agent_name),
                request_id=ctx.request_id or request_id,
                run_id=ctx.run_id or "",
                task_id=ctx.task_id or "",
            ),
            allow_apply=policy.allow_persistent_apply,
            **trigger,
        ),
    )
    suggestion = cycle["suggestion"]
    trigger_payload = dict(cycle.get("trigger", {}))
    auto_continue = _should_auto_continue_after_cycle(ctx, trigger_payload, cycle)
    status = str(cycle["status"])
    next_action = str(cycle["next_action"])
    if status == COMPACT_STATUS_READY_AFTER_ACTION_GUARD and not auto_continue:
        status = "applied_return_result"
        next_action = "return_result_after_compact"
    return _compact_auto_cycle_result_fields(
        cycle,
        trigger_payload,
        {"status": status, "next_action": next_action, "auto_continue": auto_continue},
    )


def _compact_auto_cycle_result_fields(
    cycle: dict[str, object],
    trigger_payload: dict[str, object],
    final_state: dict[str, object],
) -> dict:
    suggestion = cycle["suggestion"]
    auto_continue = bool(final_state["auto_continue"])
    return {
        "memory_compact_suggested": bool(suggestion["should_prompt"]),
        "memory_compact_status": str(suggestion["status"]),
        "memory_compact_ratio": float(suggestion["token_budget"]["ratio"]),
        "memory_compact_message": str(suggestion["message"]),
        "memory_compact_commands": list(suggestion["recommended_commands"]),
        "memory_compact_trigger_reason": str(trigger_payload.get("reason") or "normal_threshold"),
        "memory_compact_trigger_source": str(trigger_payload.get("source") or "token_budget"),
        "memory_compact_trigger_forced": bool(trigger_payload.get("forced")),
        "memory_compact_auto_status": str(final_state["status"]),
        "memory_compact_auto_next_action": str(final_state["next_action"]),
        "memory_compact_auto_allowed_to_continue": auto_continue,
        "memory_compact_auto_tool_execution": str(cycle["automatic_tool_execution"]),
        "memory_compact_auto_apply_id": str(cycle["apply_id"]),
        "memory_compact_auto_continue_ready": auto_continue and bool(cycle["continue_packet"].get("ready_to_continue")),
        "memory_compact_auto_continue_packet": dict(cycle["continue_packet"]),
    }


def _should_return_after_continuation(ctx: FinalizeContext, trigger: dict[str, object]) -> bool:
    if int(ctx.compact_auto_continue_depth or 0) <= 0:
        return False
    if _trigger_requires_continuation(trigger):
        return int(ctx.compact_auto_no_tool_continue_depth or 0) >= _MAX_CONSECUTIVE_NO_TOOL_PREFLIGHT_CONTINUATIONS
    return int(ctx.tool_rounds or 0) <= 0 and not list(ctx.executed_tools or [])


def _should_auto_continue_after_cycle(ctx: FinalizeContext, trigger_payload: dict[str, object], cycle: dict[str, object]) -> bool:
    if _delivery_complete(ctx.final_response):
        return False
    if not bool(cycle.get("allowed_to_continue")):
        return False
    if _continuation_made_tool_progress(ctx):
        return True
    if bool(trigger_payload.get("forced")):
        return True
    source = _runtime_code(trigger_payload.get("source"))
    reason = _runtime_code(trigger_payload.get("reason"))
    status = _runtime_code(getattr(ctx.final_response, "runtime_status", ""))
    runtime_reason = _runtime_code(getattr(ctx.final_response, "runtime_reason", ""))
    runtime_source = _runtime_code(getattr(ctx.final_response, "runtime_source", ""))
    return (
        source in {"preflight", "provider_error", "runtime_status"}
        or runtime_source in {"preflight", "provider_error", "runtime_status"}
        or reason in _CONTEXT_OVERFLOW_REASONS
        or status in _CONTEXT_OVERFLOW_REASONS
        or runtime_reason in _CONTEXT_OVERFLOW_REASONS
    )


def _trigger_requires_continuation(trigger: dict[str, object]) -> bool:
    if bool(trigger.get("force_trigger")):
        return True
    source = _runtime_code(trigger.get("trigger_source") or trigger.get("source"))
    reason = _runtime_code(trigger.get("trigger_reason") or trigger.get("reason"))
    return source in {"preflight", "provider_error", "runtime_status"} or reason in _CONTEXT_OVERFLOW_REASONS


def _continuation_made_tool_progress(ctx: FinalizeContext) -> bool:
    return int(ctx.tool_rounds or 0) > 0 or bool(list(ctx.executed_tools or []))


def _compact_auto_continuation_return_fields() -> dict:
    return {
        "memory_compact_suggested": False,
        "memory_compact_status": "ok",
        "memory_compact_ratio": 0.0,
        "memory_compact_message": "compact continuation returned after one no-tool model turn",
        "memory_compact_commands": [],
        "memory_compact_trigger_reason": "continuation_no_tool_progress",
        "memory_compact_trigger_source": "auto_compact",
        "memory_compact_trigger_forced": False,
        "memory_compact_auto_status": "returned_after_continuation",
        "memory_compact_auto_next_action": "return_result",
        "memory_compact_auto_allowed_to_continue": False,
        "memory_compact_auto_tool_execution": "none",
        "memory_compact_auto_apply_id": "",
        "memory_compact_auto_continue_ready": False,
        "memory_compact_auto_continue_packet": {},
    }


def _compact_auto_delivery_complete_fields() -> dict:
    return {
        "memory_compact_suggested": False,
        "memory_compact_status": "ok",
        "memory_compact_ratio": 0.0,
        "memory_compact_message": "delivery complete; compact auto continuation skipped",
        "memory_compact_commands": [],
        "memory_compact_trigger_reason": "delivery_complete",
        "memory_compact_trigger_source": "delivery_closeout",
        "memory_compact_trigger_forced": False,
        "memory_compact_auto_status": "skipped_after_delivery_complete",
        "memory_compact_auto_next_action": "return_result",
        "memory_compact_auto_allowed_to_continue": False,
        "memory_compact_auto_tool_execution": "none",
        "memory_compact_auto_apply_id": "",
        "memory_compact_auto_continue_ready": False,
        "memory_compact_auto_continue_packet": {},
    }


def _compact_trigger_from_runtime(ctx: FinalizeContext) -> dict[str, object]:
    status = _runtime_code(getattr(ctx.final_response, "runtime_status", ""))
    reason = _runtime_code(getattr(ctx.final_response, "runtime_reason", ""))
    source = _runtime_code(getattr(ctx.final_response, "runtime_source", ""))
    if status in _CONTEXT_OVERFLOW_REASONS or reason in _CONTEXT_OVERFLOW_REASONS:
        return {
            "trigger_reason": "provider_context_overflow",
            "trigger_source": source or "runtime_status",
            "force_trigger": True,
        }
    return {
        "trigger_reason": "normal_threshold",
        "trigger_source": "token_budget",
        "force_trigger": False,
    }


def _runtime_code(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _delivery_complete(final_response: object) -> bool:
    return _DELIVERY_COMPLETE_MARKER in str(getattr(final_response, "text", "") or "")


__all__ = ["compact_auto_cycle_fields"]
