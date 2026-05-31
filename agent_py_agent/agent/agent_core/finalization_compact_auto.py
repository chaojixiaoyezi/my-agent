# LLM: Finalization compact-auto projection keeps auto compact fields out of the main finalizer file.
# 模块用途: 负责 run 收尾时的自动 compact/resume 字段组装和续跑轮跳过策略。

from __future__ import annotations

from ..memory_archive import run_memory_compact_auto_cycle
from ..memory_archive.compact import MemoryCompactPlanOptions
from ..memory_archive.compact_auto import MemoryCompactAutoCycleOptions
from ._runtime_params import FinalizeContext
from .runtime_context_compactor import runtime_compact_policy
from .runtime_owner_roots import runtime_owner_root

_CONTEXT_OVERFLOW_REASONS = {
    "blackbox_output_overflow",
    "context_length_exceeded",
    "context_overflow",
    "maximum_context_length",
    "tool_output_context_overflow",
}


# LLM: compact_auto_cycle_fields honors do_save before any compact apply write.
# 函数用途: 在 run 收尾时触发自动 compact/resume 协调器；保存型运行默认走自动 apply，`save=False` 只做计划不写产物。
def compact_auto_cycle_fields(agent, ctx: FinalizeContext, token_ledger: dict[str, int], *, request_id: str = "") -> dict:
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
    if status == "ready_after_action_guard" and not auto_continue:
        status = "applied_return_result"
        next_action = "return_result_after_compact"
    return {
        "memory_compact_suggested": bool(suggestion["should_prompt"]),
        "memory_compact_status": str(suggestion["status"]),
        "memory_compact_ratio": float(suggestion["token_budget"]["ratio"]),
        "memory_compact_message": str(suggestion["message"]),
        "memory_compact_commands": list(suggestion["recommended_commands"]),
        "memory_compact_trigger_reason": str(trigger_payload.get("reason") or "normal_threshold"),
        "memory_compact_trigger_source": str(trigger_payload.get("source") or "token_budget"),
        "memory_compact_trigger_forced": bool(trigger_payload.get("forced")),
        "memory_compact_auto_status": status,
        "memory_compact_auto_next_action": next_action,
        "memory_compact_auto_allowed_to_continue": auto_continue,
        "memory_compact_auto_tool_execution": str(cycle["automatic_tool_execution"]),
        "memory_compact_auto_apply_id": str(cycle["apply_id"]),
        "memory_compact_auto_continue_ready": auto_continue and bool(cycle["continue_packet"].get("ready_to_continue")),
        "memory_compact_auto_continue_packet": dict(cycle["continue_packet"]),
    }


# LLM: A compact continuation turn that made no tool progress should return to the caller, not recursively compact itself.
# 函数用途: 去掉次数门后仍避免“恢复提示自身”无限 compact；续接轮只要有真实工具进展，就允许再次 compact 续跑。
def _should_return_after_continuation(ctx: FinalizeContext, trigger: dict[str, object]) -> bool:
    if int(ctx.compact_auto_continue_depth or 0) <= 0:
        return False
    return int(ctx.tool_rounds or 0) <= 0 and not list(ctx.executed_tools or [])


# LLM: Normal final answers may compact for future recovery but must not reopen a completed turn.
# 函数用途: 只有真正的上下文中断/撞墙 compact 才自动续跑；普通阈值 compact 只保存恢复包并返回结果。
def _should_auto_continue_after_cycle(ctx: FinalizeContext, trigger_payload: dict[str, object], cycle: dict[str, object]) -> bool:
    if not bool(cycle.get("allowed_to_continue")):
        return False
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


# LLM: _compact_auto_continuation_return_fields marks a no-tool continuation as complete without another compact loop.
# 函数用途: 生成“本轮续接已返回”的结构化字段，避免旧的次数门拆掉后出现递归 compact。
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


# LLM: _compact_trigger_from_runtime maps provider overflow signals into the same auto compact cycle.
# 函数用途: 正常阈值和 provider 上下文溢出共用一套 compact/apply/resume 链路，只用 trigger 字段区分来源。
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


# LLM: _runtime_code normalizes provider status strings for compact overflow matching.
# 函数用途: 把不同 provider 的错误状态规整成小写下划线格式。
def _runtime_code(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


__all__ = ["compact_auto_cycle_fields"]
