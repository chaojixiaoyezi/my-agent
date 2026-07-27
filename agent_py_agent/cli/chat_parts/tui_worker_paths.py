
from __future__ import annotations

import re
import time

from ...agent.concurrency.interrupt import register_interruptible
from ...agent.conversation.control_commands import (
    conversation_request_interrupt_name,
    conversation_task_attributes,
)
from ...agent.gateway_parts.response_renderer import current_context_token_estimate
from .gateway_client import (
    ChatRequestContent,
    GatewayChunkPollRequest,
    check_gateway_alive,
    poll_gateway_chunks,
    submit_chat_request,
)
from .renderer import GRAY, RESET, strip_ansi
from .tui_worker_stream import (
    _flush_stream_buf,
    _maybe_record_response,
    _set_thinking_line,
    _update_response_state,
    resume_context_override,
)


def _worker_gateway_path(ctx) -> tuple[str, bool]:
    if not check_gateway_alive(ctx.cfg.paths):
        raise RuntimeError("gateway 已停止。请先执行 my-agent gateway start")
    request_id, chunk_path, response_path = _submit_gateway_job(ctx)
    timeout = _gateway_timeout(ctx.cfg)
    chunks_printed_ref = [0]
    visible_chunks_ref = [0]
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            time.time() + max(0.0, timeout),
            ctx.on_stream_chunk,
            chunks_printed_ref,
            visible_chunks_ref,
        )
    )
    if response:
        _flush_stream_buf(ctx.cfg.stream_buf_ref)
    if not response:
        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id} response={response_path}")
    ctx.stop_spinner()
    return _finish_gateway_response(ctx, request_id, response, visible_chunks_ref[0] > 0)


def _submit_gateway_job(ctx):
    return submit_chat_request(
        ctx.cfg.paths,
        content=ChatRequestContent(
            prompt=ctx.job.user,
            inject=ctx.turn_inject,
            prompt_files=ctx.job.prompt_files,
            save=not ctx.cfg.args.no_save,
            show_prompt=ctx.job.show_prompt,
            resume_context=resume_context_override(ctx.cfg.args),
            chat_session_id=ctx.cfg.current_session_id,
            system_task=ctx.job.system_task,
        ),
        agent=ctx.cfg.agent,
    )


def _gateway_timeout(cfg) -> float:
    if cfg.args.gateway_timeout is not None:
        return cfg.args.gateway_timeout
    return cfg.agent.config.gateway_request_timeout


def _finish_gateway_response(
    ctx,
    request_id: str,
    response: dict,
    stream_has_visible_text: bool,
) -> tuple[str, bool]:
    from .rendering import _cprint

    if ctx.job.show_prompt and response.get("prompt"):
        _cprint("===== FINAL PROMPT =====")
        _cprint(response.get("prompt", ""))
        _cprint("===== RESPONSE =====")
    if not response.get("ok"):
        _cprint(f"错误: {response.get('error', 'gateway 请求失败')}")
        _print_gateway_timing(ctx, request_id, response)
        return "", False
    agent_response_text = _update_response_state(
        response, ctx.cfg.state_lock, ctx.cfg.last_token_estimate_ref
    )
    response_recorded = _record_gateway_response(
        ctx, agent_response_text, stream_has_visible_text
    )
    _print_gateway_timing(ctx, request_id, response)
    return agent_response_text, response_recorded


def _record_gateway_response(ctx, agent_response_text: str, stream_has_visible_text: bool) -> bool:
    if not agent_response_text or not agent_response_text.strip():
        return False
    if not stream_has_visible_text:
        return _maybe_record_response(
            agent_response_text, stream_has_visible_text, ctx.cfg.assistant_outputs, ctx.cfg.agent
        )
    if not _stream_output_contains_response(ctx.cfg, agent_response_text):
        from .plain_ui import AssistantResponseRenderRequest, _render_assistant_response

        _render_assistant_response(
            AssistantResponseRenderRequest(
                text=agent_response_text,
                assistant_outputs=ctx.cfg.assistant_outputs,
                agent_name=ctx.cfg.agent.config.agent_name,
            )
        )
        return True
    ctx.cfg.assistant_outputs.append(agent_response_text)
    return True


def _stream_output_contains_response(cfg, response_text: str) -> bool:
    streamed_text_ref = getattr(cfg, "stream_visible_text_ref", [""])
    streamed_text = streamed_text_ref[0] if streamed_text_ref else ""
    streamed_norm = _compact_visible_text(streamed_text)
    response_norm = _compact_visible_text(response_text)
    return bool(response_norm and response_norm in streamed_norm)


def _compact_visible_text(text: str) -> str:
    return re.sub(r"\s+", "", strip_ansi(text or ""))


def _print_gateway_timing(ctx, request_id: str, response: dict) -> None:
    elapsed = time.perf_counter() - ctx.started_at
    _publish_timing(
        ctx,
        f"[耗时 {elapsed:.2f}s; "
        f"工具轮数 {response.get('tool_rounds', 0)}; "
        f"ctx_tokens~{current_context_token_estimate(response)}; "
        f"prompt_tokens~{response.get('prompt_token_estimate', 0)}; "
        f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]",
    )


def _worker_local_path(ctx) -> tuple[str, bool]:
    from .plain_ui import AssistantResponseRenderRequest, _render_assistant_response

    with register_interruptible(conversation_request_interrupt_name(ctx.job.request_id)):
        result = ctx.cfg.agent.run(
            ctx.job.user,
            inject=ctx.turn_inject,
            prompt_files=ctx.job.prompt_files,
            save=not ctx.cfg.args.no_save,
            request_id=ctx.job.request_id,
            source="chat",
            resume_context=resume_context_override(ctx.cfg.args),
            recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
            on_chunk=ctx.on_stream_chunk,
            task_attributes=conversation_task_attributes(ctx.job.system_task),
        )
    ctx.stop_spinner()
    _flush_stream_buf(ctx.cfg.stream_buf_ref)
    _print_local_timing(ctx, result)
    with ctx.cfg.state_lock:
        ctx.cfg.last_token_estimate_ref[0] = current_context_token_estimate(result)
    if result.response.strip():
        stream_has_visible_text = bool(strip_ansi(ctx.cfg.stream_visible_text_ref[0]).strip())
        if stream_has_visible_text and _stream_output_contains_response(ctx.cfg, result.response):
            ctx.cfg.assistant_outputs.append(result.response)
            return result.response, True
        _render_assistant_response(
            AssistantResponseRenderRequest(
                text=result.response,
                assistant_outputs=ctx.cfg.assistant_outputs,
                agent_name=ctx.cfg.agent.config.agent_name,
            )
        )
        return result.response, True
    return result.response, False


def _print_local_timing(ctx, result) -> None:
    from .rendering import _cprint

    if ctx.job.show_prompt:
        _cprint("===== FINAL PROMPT =====")
        _cprint(result.prompt)
        _cprint("===== RESPONSE =====")
    elapsed = time.perf_counter() - ctx.started_at
    _publish_timing(
        ctx,
        f"[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
        f"ctx_tokens~{current_context_token_estimate(result)}; "
        f"prompt_tokens~{result.prompt_token_estimate}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}]",
    )


def _publish_timing(ctx, text: str) -> None:
    if _use_app_status_line(getattr(ctx.cfg, "args", None)):
        _set_thinking_line(text, ctx.cfg.thinking_line_ref)
        if ctx.cfg.app_ref[0] is not None:
            ctx.cfg.app_ref[0].invalidate()
        return
    from .rendering import _cprint

    _cprint(f"{GRAY}{text}{RESET}")


def _use_app_status_line(args) -> bool:
    if args is None:
        return False
    if getattr(args, "plain", False):
        return False
    return bool(getattr(args, "app_scrollback", True))


__all__ = ["_worker_gateway_path", "_worker_local_path"]
