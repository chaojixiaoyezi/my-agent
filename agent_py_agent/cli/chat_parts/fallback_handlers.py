from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .fallback_state import _CHAT_RESPONSE_STYLE_INJECT, resume_context_override
from .fallback_ui import _make_chunk_handler, _render_assistant_response
from .gateway_client import (
    ChatRequestContent,
    GatewayChunkPollRequest,
    GatewayTimingContext,
    check_gateway_alive,
    format_gateway_timing,
    poll_gateway_chunks,
    submit_chat_request,
)
from .rendering import GRAY, RESET, _cprint


@dataclass
class FallbackJobContext:
    job: object
    agent: object
    args: object
    paths: object
    assistant_outputs: list[str]
    build_history_context: Callable[[], str]


def _fallback_gateway_handle(ctx: FallbackJobContext) -> tuple[str, bool]:
    if not check_gateway_alive(ctx.paths):
        raise RuntimeError("gateway 已停止。请先执行 my-agent gateway start")
    started_at = time.perf_counter()
    on_chunk, stream_started_ref = _make_chunk_handler(
        ctx.agent.config.agent_name, _next_message_id(ctx)
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
        ),
        agent=ctx.agent,
    )
    response = poll_gateway_chunks(GatewayChunkPollRequest(chunk_path, response_path, _gateway_deadline(ctx), on_chunk, [0]))
    response_text = response.get("response", "")
    if not response and request_id:
        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id}")
    _render_if_needed(ctx, response_text, stream_started_ref[0])
    _print_gateway_timing(request_id, started_at, response, ctx)
    return response_text, stream_started_ref[0]


def _fallback_local_handle(ctx: FallbackJobContext) -> tuple[str, bool]:
    started_at = time.perf_counter()
    on_chunk, stream_started_ref = _make_chunk_handler(
        ctx.agent.config.agent_name, _next_message_id(ctx)
    )
    result = ctx.agent.run(
        ctx.job.user,
        inject=_turn_inject(ctx),
        prompt_files=ctx.job.prompt_files,
        save=not ctx.args.no_save,
        source="chat",
        resume_context=resume_context_override(ctx.args),
        recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
        on_chunk=on_chunk,
    )
    agent_response_text = result.response
    _render_if_needed(ctx, agent_response_text, stream_started_ref[0])
    _print_local_timing(result, started_at)
    return agent_response_text, stream_started_ref[0]


def _next_message_id(ctx: FallbackJobContext) -> int:
    return len(ctx.assistant_outputs) + 1


def _turn_inject(ctx: FallbackJobContext) -> list[str]:
    history_ctx = ctx.build_history_context()
    turn_inject = list(ctx.job.inject) + [_CHAT_RESPONSE_STYLE_INJECT]
    if history_ctx:
        turn_inject.append(history_ctx)
    return turn_inject


def _gateway_deadline(ctx: FallbackJobContext) -> float:
    timeout = getattr(ctx.args, "gateway_timeout", None)
    if timeout is None:
        timeout = ctx.agent.config.gateway_request_timeout
    return time.time() + max(0.0, timeout)


def _render_if_needed(
    ctx: FallbackJobContext, response_text: str, stream_started: bool
) -> None:
    if not stream_started:
        _render_assistant_response(response_text, ctx.assistant_outputs, ctx.agent.config.agent_name)


def _print_gateway_timing(
    request_id: str,
    started_at: float,
    response: dict,
    ctx: FallbackJobContext,
) -> None:
    elapsed = time.perf_counter() - started_at
    _cprint(
        f"{GRAY}{format_gateway_timing(GatewayTimingContext(request_id, elapsed, response, True))}{RESET}"
    )


def _print_local_timing(result, started_at: float) -> None:
    elapsed = time.perf_counter() - started_at
    _cprint(
        f"{GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
        f"prompt_tokens~{result.prompt_token_estimate}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}]{RESET}"
    )


__all__ = [
    "FallbackJobContext",
    "_fallback_gateway_handle",
    "_fallback_local_handle",
]
