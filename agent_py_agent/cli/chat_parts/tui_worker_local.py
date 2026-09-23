# LLM: 本模块只服务 direct TUI，本地 Agent、模型、工具和中断依赖不得被 Gateway TUI 导入。
# 模块用途: 执行一次本地主代理聊天回合，并把结果转换成与 Gateway 相同的 TUI 终态。

from __future__ import annotations

from typing import Any

from ...agent.agent_core.runtime.loop_models import RunParams
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
from .tui_worker_paths import _model_length_error, _nonnegative_int


# LLM: local adapter 交给 core 同一个 worker 句柄；模型前绑定真实身份，启动前中断不能随线程注册丢失。
# 函数用途: 验证媒体后执行本地回合，返回完整或被截断的实际回复与明确终态提示。
def _worker_local_path(ctx: Any) -> tuple[str, bool, TuiTurnSummary]:
    from ...agent.conversation.input_media import input_media_root, validate_input_media

    attrs = conversation_task_attributes(ctx.job.system_task)
    if ctx.job.input_media:
        refs = validate_input_media(ctx.job.input_media, root=input_media_root(ctx.cfg.agent),
                                    max_bytes=ctx.cfg.agent.config.input_media_max_bytes,
                                    max_files=ctx.cfg.agent.config.input_media_max_files)
        attrs = {**attrs, "input_media": list(refs)}
    control = ctx.cfg.local_run_ref[0]
    if control is None or control.request_id != ctx.job.request_id:
        raise ValueError("本地 TUI 缺少本轮控制句柄")
    with register_interruptible(conversation_request_interrupt_name(ctx.job.request_id)):
        control.check_admission()
        result = ctx.cfg.agent.run(
            ctx.job.user,
            params=RunParams(conversation_task_binding_callback=control),
            inject=ctx.turn_inject,
            prompt_files=ctx.job.prompt_files,
            save=ctx.job.save,
            request_id=ctx.job.request_id,
            source="chat",
            resume_context=ctx.job.resume_context,
            recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
            on_chunk=ctx.turn_adapter,
            task_attributes=attrs,
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
    error = _model_length_error(result)
    ok = not error and runtime_status not in {"failed", "error", "cancelled"}
    error = error or ("" if ok else str(getattr(result, "runtime_reason", "") or runtime_status))
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
