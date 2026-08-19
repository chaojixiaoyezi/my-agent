# LLM: 本模块只服务 direct TUI，本地 Agent、模型、工具和中断依赖不得被 Gateway TUI 导入。
# 模块用途: 执行一次本地主代理聊天回合，并把结果转换成与 Gateway 相同的 TUI 终态。

from __future__ import annotations

from typing import Any

from ...agent.concurrency.interrupt import register_interruptible
from ...agent.conversation.control_commands import (
    conversation_request_interrupt_name,
    conversation_task_attributes,
)
from ...agent.gateway_parts.response_renderer import (
    current_context_token_estimate,
    is_silent_user_stop,
)
from ...agent.memory_archive.tokens import estimate_tokens
from .tui_runtime import TuiTurnSummary
from .tui_worker_paths import _nonnegative_int


# LLM: local path 把 adapter 原样交给 agent.run，使模型 delta 与 structured tool progress 共用稳定 block identity。
# 函数用途: 执行一次本地主代理回合并返回回复与结构化 TUI 终态。
def _worker_local_path(ctx: Any) -> tuple[str, bool, TuiTurnSummary]:
    with register_interruptible(conversation_request_interrupt_name(ctx.job.request_id)):
        result = ctx.cfg.agent.run(
            ctx.job.user,
            inject=ctx.turn_inject,
            prompt_files=ctx.job.prompt_files,
            save=ctx.job.save,
            request_id=ctx.job.request_id,
            source="chat",
            resume_context=ctx.job.resume_context,
            recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
            on_chunk=ctx.turn_adapter,
            task_attributes=conversation_task_attributes(ctx.job.system_task),
        )
    if is_silent_user_stop(result):
        return "", False, TuiTurnSummary(ok=False, interrupted=True)
    if ctx.job.show_prompt:
        ctx.cfg.tui_runtime.write_console(
            "===== FINAL PROMPT =====\n"
            + str(result.prompt or "")
            + "\n===== RESPONSE ====="
        )
    text = str(result.response or "")
    context_tokens = current_context_token_estimate(result)
    with ctx.cfg.state_lock:
        ctx.cfg.last_token_estimate_ref[0] = context_tokens
    runtime_status = str(getattr(result, "runtime_status", "ok") or "ok").lower()
    ok = runtime_status not in {"failed", "error", "cancelled"}
    error = "" if ok else str(getattr(result, "runtime_reason", "") or runtime_status)
    summary = TuiTurnSummary(
        response_text=text,
        ok=ok,
        error=error,
        context_tokens=context_tokens,
        output_tokens=estimate_tokens(text) if text else 0,
        tool_rounds=_nonnegative_int(getattr(result, "tool_rounds", 0)),
    )
    return text, False, summary


__all__ = ["_worker_local_path"]
