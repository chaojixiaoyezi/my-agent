from __future__ import annotations

from ..contracts.protocol_status import COMPACT_STATUS_READY_AFTER_ACTION_GUARD

"""safe coordinator for an automated compact/resume cycle."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact import MemoryCompactPlanOptions
from .compact_apply import MemoryCompactApplyOptions, apply_memory_compact
from .compact_circuit_breaker import (
    compact_circuit_open,
    read_compact_circuit,
    record_compact_outcome,
)
from .compact_resume import MemoryCompactResumeOptions, build_memory_compact_resume
from .compact_suggest import MemoryCompactSuggestOptions, build_memory_compact_suggestion
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)

COMPACT_AUTO_CYCLE_SCHEMA = RuntimeMemorySchemaOptions("compact_auto_cycle")


@dataclass(frozen=True)
class MemoryCompactAutoCycleOptions:
    current_tokens: int
    max_context_tokens: int
    plan_options: MemoryCompactPlanOptions
    # 与 AgentConfig / 包内 YAML 的正式默认保持一致；50 仅由压力测试显式覆盖。
    trigger_percent: int = 90
    allow_apply: bool = False
    owner_type: str = "main_agent"
    owner_id: str = ""
    #  只用 trigger 标记为什么进入 compact；不为强制触发另建第二套 apply/resume 流程。
    trigger_reason: str = "normal_threshold"
    trigger_source: str = "token_budget"
    force_trigger: bool = False
    now: float = 0.0  # 熔断时间基准；0 表示用真实 time.time()（仅测试注入）


@dataclass(frozen=True)
class _AutoCyclePayloadOptions:
    workspace: Path
    options: MemoryCompactAutoCycleOptions
    suggestion: dict[str, Any]
    status: str
    apply_result: dict[str, Any] | None = None
    resume: dict[str, Any] | None = None


def run_memory_compact_auto_cycle(root: str | Path, options: MemoryCompactAutoCycleOptions) -> dict[str, Any]:
    workspace = Path(root)
    suggestion = _suggestion(workspace, options)
    if not suggestion["should_prompt"]:
        return _cycle_payload(_AutoCyclePayloadOptions(workspace, options, suggestion, "skipped_below_threshold"))
    if not options.allow_apply:
        return _cycle_payload(_AutoCyclePayloadOptions(workspace, options, suggestion, "needs_user_confirmation"))
    now = options.now or _compact_now()
    if compact_circuit_open(read_compact_circuit(workspace), now=now):
        # 连续失败熔断中：跳过自动 compact，让上层 stop_and_request_review，不空烧重试。
        return _cycle_payload(_AutoCyclePayloadOptions(workspace, options, suggestion, "blocked_circuit_open"))
    apply_result = apply_memory_compact(workspace, MemoryCompactApplyOptions(plan_options=options.plan_options))
    resume = build_memory_compact_resume(
        workspace,
        MemoryCompactResumeOptions(
            apply_ref=str(apply_result["apply_id"]),
            owner_type=options.owner_type,
            owner_id=options.owner_id,
            resume_mode="auto",
        ),
    )
    status = COMPACT_STATUS_READY_AFTER_ACTION_GUARD if resume["action_guard"]["allowed_to_continue"] else "blocked_after_action_guard"
    record_compact_outcome(workspace, ok=not str(status).startswith("blocked"), status=status, now=now)
    return _cycle_payload(
        _AutoCyclePayloadOptions(workspace, options, suggestion, status, apply_result=apply_result, resume=resume)
    )


def _compact_now() -> float:
    import time

    return time.time()


def _suggestion(workspace: Path, options: MemoryCompactAutoCycleOptions) -> dict[str, Any]:
    return build_memory_compact_suggestion(
        workspace,
        MemoryCompactSuggestOptions(
            current_tokens=options.current_tokens,
            max_context_tokens=options.max_context_tokens,
            plan_options=options.plan_options,
            trigger_percent=options.trigger_percent,
            owner_type=options.owner_type,
            owner_id=options.owner_id,
            trigger_reason=options.trigger_reason,
            trigger_source=options.trigger_source,
            force_trigger=options.force_trigger,
        ),
    )


def _cycle_payload(request: _AutoCyclePayloadOptions) -> dict[str, Any]:
    resume = request.resume
    return {
        "version": COMPACT_AUTO_CYCLE_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_AUTO_CYCLE_SCHEMA),
        "ok": not str(request.status).startswith("blocked"),
        "event_type": "compact_auto_cycle",
        "mode": "auto_cycle",
        "status": request.status,
        "workspace_root": str(request.workspace),
        "allow_apply": request.options.allow_apply,
        "automatic_tool_execution": "none",
        "owner": {"owner_type": request.options.owner_type, "owner_id": request.options.owner_id},
        "trigger": dict(request.suggestion.get("trigger", {})),
        "suggestion": request.suggestion,
        "apply_result": request.apply_result or {},
        "resume_result": resume or {},
        "continue_packet": _continue_packet(resume),
        "apply_id": _apply_id(request.apply_result),
        "allowed_to_continue": bool(resume and resume["action_guard"]["allowed_to_continue"]),
        "next_action": _next_action(request.status),
    }


def _continue_packet(resume: dict[str, Any] | None) -> dict[str, Any]:
    if not resume:
        return {}
    packet = resume.get("continue_packet", {})
    return packet if isinstance(packet, dict) else {}


def _apply_id(apply_result: dict[str, Any] | None) -> str:
    return str(apply_result.get("apply_id", "") or "") if apply_result else ""


def _next_action(status: str) -> str:
    if status == "skipped_below_threshold":
        return "continue_without_compact"
    if status == "needs_user_confirmation":
        return "ask_user_before_apply"
    if status == COMPACT_STATUS_READY_AFTER_ACTION_GUARD:
        return "continue_after_guard"
    return "stop_and_request_review"


__all__ = ["MemoryCompactAutoCycleOptions", "run_memory_compact_auto_cycle"]
