
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ...agent.concurrency.interrupt import register_interruptible
from ...agent.conversation.control_commands import (
    conversation_request_interrupt_name,
    conversation_task_attributes,
)
from ...agent.gateway_parts.response_renderer import (
    current_context_token_estimate,
    is_silent_user_stop,
)
from .gateway_client import (
    ChatRequestContent,
    GatewayChunkPollRequest,
    GatewayTimingContext,
    check_gateway_alive,
    format_gateway_timing,
    gateway_request_activity_paths,
    poll_gateway_chunks,
    submit_chat_request,
)
from .plain_state import _CHAT_RESPONSE_STYLE_INJECT, resume_context_override
from .plain_ui import _make_chunk_handler, _render_assistant_response
from .rendering import GRAY, RESET, _cprint


@dataclass
class PlainJobContext:
    job: object
    agent: object
    args: object
    paths: object
    assistant_outputs: list[str]
    build_history_context: Callable[[], str]
    current_session_id: str = ""


def _plain_gateway_handle(ctx: PlainJobContext) -> tuple[str, bool]:
    if not check_gateway_alive(ctx.paths):
        raise RuntimeError("gateway 已停止。请先执行 my-agent gateway start")
    started_at = time.perf_counter()
    on_chunk, stream_started_ref = _make_chunk_handler(
        ctx.agent.config.agent_name,
        _next_message_id(ctx),
        preview_chars=_chat_preview_chars(ctx),
    )
    request_id, chunk_path, response_path = submit_chat_request(
        ctx.paths,
        content=ChatRequestContent(
            prompt=ctx.job.user,
            inject=_turn_inject(ctx),
            prompt_files=ctx.job.prompt_files,
            save=not ctx.args.no_save,
            show_prompt=ctx.job.show_prompt,
            resume_context=resume_context_override(ctx.args),
            chat_session_id=ctx.current_session_id,
            system_task=ctx.job.system_task,
        ),
        agent=ctx.agent,
    )
    terminal_response_streamed_ref = [0]
    inactivity_timeout = _gateway_timeout_seconds(ctx)
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            time.time() + inactivity_timeout,
            on_chunk,
            [0],
            terminal_response_streamed_ref,
            activity_paths=gateway_request_activity_paths(
                ctx.paths,
                request_id,
                chunk_path,
            ),
            inactivity_timeout_seconds=inactivity_timeout,
        )
    )
    response_text = response.get("response", "")
    if not response and request_id:
        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id}")
    if is_silent_user_stop(response):
        return "", False
    terminal_response_streamed = terminal_response_streamed_ref[0] > 0
    _render_if_needed(ctx, response_text, terminal_response_streamed)
    _print_gateway_timing(request_id, started_at, response, ctx)
    return response_text, terminal_response_streamed


def _plain_local_handle(ctx: PlainJobContext) -> tuple[str, bool]:
    started_at = time.perf_counter()
    on_chunk, stream_started_ref = _make_chunk_handler(
        ctx.agent.config.agent_name,
        _next_message_id(ctx),
        preview_chars=_chat_preview_chars(ctx),
    )
    with register_interruptible(conversation_request_interrupt_name(ctx.job.request_id)):
        result = ctx.agent.run(
            ctx.job.user,
            inject=_turn_inject(ctx),
            prompt_files=ctx.job.prompt_files,
            save=not ctx.args.no_save,
            request_id=ctx.job.request_id,
            source="chat",
            resume_context=resume_context_override(ctx.args),
            recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
            on_chunk=on_chunk,
            task_attributes=conversation_task_attributes(ctx.job.system_task),
        )
    agent_response_text = result.response
    if is_silent_user_stop(result):
        return "", False
    _render_if_needed(ctx, agent_response_text, stream_started_ref[0])
    _print_local_timing(result, started_at)
    return agent_response_text, stream_started_ref[0]


def _next_message_id(ctx: PlainJobContext) -> int:
    return len(ctx.assistant_outputs) + 1


def _turn_inject(ctx: PlainJobContext) -> list[str]:
    history_ctx = ctx.build_history_context()
    turn_inject = list(ctx.job.inject) + [_CHAT_RESPONSE_STYLE_INJECT]
    if history_ctx:
        turn_inject.append(history_ctx)
    return turn_inject


def _gateway_timeout_seconds(ctx: PlainJobContext) -> float:
    timeout = getattr(ctx.args, "gateway_timeout", None)
    if timeout is None:
        timeout = ctx.agent.config.gateway_request_timeout
    return max(0.0, float(timeout))


def _render_if_needed(
    ctx: PlainJobContext, response_text: str, stream_started: bool
) -> None:
    if not stream_started:
        from .plain_ui import AssistantResponseRenderRequest

        _render_assistant_response(
            AssistantResponseRenderRequest(
                text=response_text,
                assistant_outputs=ctx.assistant_outputs,
                agent_name=ctx.agent.config.agent_name,
                preview_lines=_chat_preview_lines(ctx),
                preview_chars=_chat_preview_chars(ctx),
            )
        )


def _chat_preview_lines(ctx: PlainJobContext) -> int:
    try:
        return max(0, int(getattr(ctx.agent.config, "chat_collapse_preview_lines", 12) or 0))
    except (TypeError, ValueError):
        return 12


def _chat_preview_chars(ctx: PlainJobContext) -> int:
    try:
        return max(0, int(getattr(ctx.agent.config, "chat_collapse_preview_chars", 900) or 0))
    except (TypeError, ValueError):
        return 900


def _print_gateway_timing(
    request_id: str,
    started_at: float,
    response: dict,
    ctx: PlainJobContext,
) -> None:
    elapsed = time.perf_counter() - started_at
    _cprint(
        f"{GRAY}{format_gateway_timing(GatewayTimingContext(request_id, elapsed, response, True))}{RESET}"
    )


def _print_local_timing(result, started_at: float) -> None:
    elapsed = time.perf_counter() - started_at
    _cprint(
        f"{GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
        f"ctx_tokens~{current_context_token_estimate(result)}; "
        f"prompt_tokens~{result.prompt_token_estimate}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}]{RESET}"
    )


__all__ = [
    "PlainJobContext",
    "_plain_gateway_handle",
    "_plain_local_handle",
]
