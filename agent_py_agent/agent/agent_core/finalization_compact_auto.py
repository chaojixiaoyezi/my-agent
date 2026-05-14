# LLM: Finalization compact-auto projection keeps auto compact fields out of the main finalizer file.
# 模块用途: 负责 run 收尾时的自动 compact/resume 字段组装和续跑轮跳过策略。

from __future__ import annotations

from ..memory_archive import run_memory_compact_auto_cycle
from ..memory_archive.compact import MemoryCompactPlanOptions
from ..memory_archive.compact_auto import MemoryCompactAutoCycleOptions
from ._runtime_params import FinalizeContext


# LLM: compact_auto_cycle_fields honors do_save before any opt-in compact apply write.
# 函数用途: 在 run 收尾时触发自动 compact/resume 协调器；`save=False` 时即使配置允许也只做计划，不写 apply 产物。
def compact_auto_cycle_fields(agent, ctx: FinalizeContext, token_ledger: dict[str, int], *, request_id: str = "") -> dict:
    if ctx.compact_auto_continue_depth > 0 and ctx.compact_auto_continue_depth >= max(
        0, ctx.compact_auto_continue_max_depth
    ):
        return _compact_auto_continuation_skip_fields()
    allow_apply = ctx.do_save and bool(getattr(agent.config, "memory_compact_auto_allow_apply", False))
    cycle = run_memory_compact_auto_cycle(
        agent.root,
        MemoryCompactAutoCycleOptions(
            current_tokens=int(token_ledger["cumulative"]),
            max_context_tokens=_compact_context_window_tokens(agent),
            plan_options=MemoryCompactPlanOptions(
                session_id=getattr(agent, "session_id", agent.config.agent_name),
                request_id=ctx.request_id or request_id,
                run_id=ctx.run_id or "",
                task_id=ctx.task_id or "",
            ),
            allow_apply=allow_apply,
        ),
    )
    suggestion = cycle["suggestion"]
    return {
        "memory_compact_suggested": bool(suggestion["should_prompt"]),
        "memory_compact_status": str(suggestion["status"]),
        "memory_compact_ratio": float(suggestion["token_budget"]["ratio"]),
        "memory_compact_message": str(suggestion["message"]),
        "memory_compact_commands": list(suggestion["recommended_commands"]),
        "memory_compact_auto_status": str(cycle["status"]),
        "memory_compact_auto_next_action": str(cycle["next_action"]),
        "memory_compact_auto_allowed_to_continue": bool(cycle["allowed_to_continue"]),
        "memory_compact_auto_tool_execution": str(cycle["automatic_tool_execution"]),
        "memory_compact_auto_apply_id": str(cycle["apply_id"]),
        "memory_compact_auto_continue_ready": bool(cycle["continue_packet"].get("ready_to_continue")),
        "memory_compact_auto_continue_packet": dict(cycle["continue_packet"]),
    }


# LLM: _compact_auto_continuation_skip_fields prevents a resumed turn from compacting itself again.
# 函数用途: 自动续跑轮只执行恢复后的下一步，不再次触发 compact/apply/续跑链，避免循环。
def _compact_auto_continuation_skip_fields() -> dict:
    return {
        "memory_compact_suggested": False,
        "memory_compact_status": "ok",
        "memory_compact_ratio": 0.0,
        "memory_compact_message": "auto compact skipped during guarded continuation",
        "memory_compact_commands": [],
        "memory_compact_auto_status": "skipped_after_guarded_continuation",
        "memory_compact_auto_next_action": "continue_without_compact",
        "memory_compact_auto_allowed_to_continue": False,
        "memory_compact_auto_tool_execution": "none",
        "memory_compact_auto_apply_id": "",
        "memory_compact_auto_continue_ready": False,
        "memory_compact_auto_continue_packet": {},
    }


# LLM: _compact_context_window_tokens keeps compact suggestions conservative until real context windows exist.
# 函数用途: 读取可选配置 memory_compact_context_window_tokens；未设置时用 max_tokens 的保守倍数估算窗口。
def _compact_context_window_tokens(agent) -> int:
    configured = int(getattr(agent.config, "memory_compact_context_window_tokens", 0) or 0)
    if configured > 0:
        return configured
    max_tokens = int(getattr(agent.config, "max_tokens", 1024) or 1024)
    return max(8192, max_tokens * 16)


__all__ = ["compact_auto_cycle_fields"]
