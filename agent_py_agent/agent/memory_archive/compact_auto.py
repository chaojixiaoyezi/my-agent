# LLM: Compact auto cycle coordinates suggestion/apply/resume but never continues tool execution.
# 模块用途: 生成自动 compact/resume 的安全计划；可选执行非破坏性 apply，并在 action guard 前停住。

from __future__ import annotations

"""safe coordinator for the first automated compact/resume cycle."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact import MemoryCompactPlanOptions
from .compact_apply import MemoryCompactApplyOptions, apply_memory_compact
from .compact_resume import MemoryCompactResumeOptions, build_memory_compact_resume
from .compact_suggest import MemoryCompactSuggestOptions, build_memory_compact_suggestion
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_AUTO_CYCLE_SCHEMA = RuntimeMemorySchemaOptions("compact_auto_cycle")


# LLM: MemoryCompactAutoCycleOptions keeps automated compact behavior explicit and opt-in.
# 类用途: 描述自动 compact/resume 协调器输入；默认只生成计划，不写 apply 产物。
@dataclass(frozen=True)
class MemoryCompactAutoCycleOptions:
    current_tokens: int
    max_context_tokens: int
    plan_options: MemoryCompactPlanOptions
    trigger_percent: int = 90
    allow_apply: bool = False
    owner_type: str = "main_agent"
    owner_id: str = ""
    # 参数说明: 只用 trigger 标记为什么进入 compact；不为兜底触发另建第二套 apply/resume 流程。
    trigger_reason: str = "normal_threshold"
    trigger_source: str = "token_budget"
    force_trigger: bool = False


# LLM: _AutoCyclePayloadOptions keeps internal payload assembly extensible without widening helper signatures.
# 类用途: 打包 auto cycle 输出所需的上下文、状态和可选 apply/resume 结果，只在本模块内部使用。
@dataclass(frozen=True)
class _AutoCyclePayloadOptions:
    workspace: Path
    options: MemoryCompactAutoCycleOptions
    suggestion: dict[str, Any]
    status: str
    apply_result: dict[str, Any] | None = None
    resume: dict[str, Any] | None = None


# LLM: run_memory_compact_auto_cycle is the safe automation coordinator and stops before continuing work.
# 函数用途: 串起半自动提示、可选非破坏性 apply、auto resume 和 action guard；不会执行工具或改代码。
def run_memory_compact_auto_cycle(root: str | Path, options: MemoryCompactAutoCycleOptions) -> dict[str, Any]:
    workspace = Path(root)
    suggestion = _suggestion(workspace, options)
    if not suggestion["should_prompt"]:
        return _cycle_payload(_AutoCyclePayloadOptions(workspace, options, suggestion, "skipped_below_threshold"))
    if not options.allow_apply:
        return _cycle_payload(_AutoCyclePayloadOptions(workspace, options, suggestion, "needs_user_confirmation"))
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
    status = "ready_after_action_guard" if resume["action_guard"]["allowed_to_continue"] else "blocked_after_action_guard"
    return _cycle_payload(
        _AutoCyclePayloadOptions(workspace, options, suggestion, status, apply_result=apply_result, resume=resume)
    )


# LLM: _suggestion keeps compact auto cycle aligned with the normal semi-auto prompt thresholds.
# 函数用途: 复用 compact_suggest 的 token 阈值、scope 和 owner 字段。
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


# LLM: _cycle_payload returns one stable audit shape for skipped, planned, applied, and blocked cycles.
# 函数用途: 组装自动 compact/resume 协调结果，明确是否写入 apply 产物和是否允许继续。
def _cycle_payload(request: _AutoCyclePayloadOptions) -> dict[str, Any]:
    resume = request.resume
    return {
        "version": COMPACT_AUTO_CYCLE_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_AUTO_CYCLE_SCHEMA),
        "ok": request.status not in {"blocked_after_action_guard"},
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
        "reserved": runtime_memory_reserved_fields(COMPACT_AUTO_CYCLE_SCHEMA),
    }


# LLM: _continue_packet exposes the resume continuation contract without expanding auto-cycle callers.
# 函数用途: 从 resume_result 中取出继续工作包；没有 apply/resume 时返回空对象。
def _continue_packet(resume: dict[str, Any] | None) -> dict[str, Any]:
    if not resume:
        return {}
    packet = resume.get("continue_packet", {})
    return packet if isinstance(packet, dict) else {}


# LLM: _apply_id gives run finalization a small stable ref without copying the whole apply result.
# 函数用途: 从可选 apply_result 中取 apply_id，未执行 apply 时返回空字符串。
def _apply_id(apply_result: dict[str, Any] | None) -> str:
    return str(apply_result.get("apply_id", "") or "") if apply_result else ""


# LLM: _next_action gives automation callers one machine-readable stop/continue recommendation.
# 函数用途: 根据 auto cycle 状态返回下一步建议，不靠自然语言解析。
def _next_action(status: str) -> str:
    if status == "skipped_below_threshold":
        return "continue_without_compact"
    if status == "needs_user_confirmation":
        return "ask_user_before_apply"
    if status == "ready_after_action_guard":
        return "continue_after_guard"
    return "stop_and_request_review"


__all__ = ["MemoryCompactAutoCycleOptions", "run_memory_compact_auto_cycle"]
