# LLM: Finalization compact-auto projection keeps auto compact fields out of the main finalizer file.
# 模块用途: 负责 run 收尾时的自动 compact/resume 字段组装和续跑轮跳过策略。

from __future__ import annotations

from ..memory_archive import run_memory_compact_auto_cycle
from ..memory_archive.compact import MemoryCompactPlanOptions
from ..memory_archive.compact_auto import MemoryCompactAutoCycleOptions
from ._runtime_params import FinalizeContext
from .model_context_window import resolve_model_context_window_tokens

_CONTEXT_OVERFLOW_REASONS = {
    "blackbox_output_overflow",
    "context_length_exceeded",
    "context_overflow",
    "maximum_context_length",
    "tool_output_context_overflow",
}


# LLM: compact_auto_cycle_fields honors do_save before any opt-in compact apply write.
# 函数用途: 在 run 收尾时触发自动 compact/resume 协调器；`save=False` 时即使配置允许也只做计划，不写 apply 产物。
def compact_auto_cycle_fields(agent, ctx: FinalizeContext, token_ledger: dict[str, int], *, request_id: str = "") -> dict:
    trigger = _compact_trigger_from_runtime(ctx)
    if _should_return_after_continuation(ctx, trigger):
        return _compact_auto_continuation_return_fields()
    allow_apply = ctx.do_save and bool(getattr(agent.config, "memory_compact_auto_allow_apply", False))
    cycle = run_memory_compact_auto_cycle(
        agent.root,
        MemoryCompactAutoCycleOptions(
            current_tokens=int(token_ledger.get("active", token_ledger["turn"])),
            max_context_tokens=_compact_context_window_tokens(agent),
            trigger_percent=int(getattr(agent.config, "memory_compact_auto_trigger_percent", 90) or 100),
            plan_options=MemoryCompactPlanOptions(
                session_id=getattr(agent, "session_id", agent.config.agent_name),
                request_id=ctx.request_id or request_id,
                run_id=ctx.run_id or "",
                task_id=ctx.task_id or "",
            ),
            allow_apply=allow_apply,
            **trigger,
        ),
    )
    suggestion = cycle["suggestion"]
    trigger_payload = dict(cycle.get("trigger", {}))
    return {
        "memory_compact_suggested": bool(suggestion["should_prompt"]),
        "memory_compact_status": str(suggestion["status"]),
        "memory_compact_ratio": float(suggestion["token_budget"]["ratio"]),
        "memory_compact_message": str(suggestion["message"]),
        "memory_compact_commands": list(suggestion["recommended_commands"]),
        "memory_compact_trigger_reason": str(trigger_payload.get("reason") or "normal_threshold"),
        "memory_compact_trigger_source": str(trigger_payload.get("source") or "token_budget"),
        "memory_compact_trigger_forced": bool(trigger_payload.get("forced")),
        "memory_compact_auto_status": str(cycle["status"]),
        "memory_compact_auto_next_action": str(cycle["next_action"]),
        "memory_compact_auto_allowed_to_continue": bool(cycle["allowed_to_continue"]),
        "memory_compact_auto_tool_execution": str(cycle["automatic_tool_execution"]),
        "memory_compact_auto_apply_id": str(cycle["apply_id"]),
        "memory_compact_auto_continue_ready": bool(cycle["continue_packet"].get("ready_to_continue")),
        "memory_compact_auto_continue_packet": dict(cycle["continue_packet"]),
    }


# LLM: A compact continuation turn that made no tool progress should return to the caller, not recursively compact itself.
# 函数用途: 去掉次数门后仍避免“恢复提示自身”因为普通阈值无限 compact；真实 provider overflow 仍走兜底 compact。
def _should_return_after_continuation(ctx: FinalizeContext, trigger: dict[str, object]) -> bool:
    if int(ctx.compact_auto_continue_depth or 0) <= 0:
        return False
    if str(trigger.get("trigger_source") or "") == "preflight":
        return True
    if bool(trigger.get("force_trigger")):
        return False
    return int(ctx.tool_rounds or 0) <= 0 and not list(ctx.executed_tools or [])


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


# LLM: _compact_context_window_tokens delegates backend metadata lookup to one shared resolver.
# 函数用途: 获取 compact 阈值窗口；只信当前模型后端元数据，未知时返回 0。
def _compact_context_window_tokens(agent) -> int:
    return resolve_model_context_window_tokens(agent)


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
