from __future__ import annotations

"""Fallback job handlers for chat mode."""

import time
from collections.abc import Callable

from .fallback_state import _CHAT_RESPONSE_STYLE_INJECT, resume_context_override
from .fallback_ui import _make_chunk_handler, _render_assistant_response
from .gateway_client import (
    ChatRequestContent,
    check_gateway_alive,
    poll_gateway_chunks,
    submit_chat_request,
)


def _fallback_gateway_handle(
    job,
    agent,
    args,
    paths,
    assistant_outputs: list[str],
    build_history_context: Callable[[], str],
) -> tuple[str, bool]:
    """Handle gateway-mode job. Returns (response_text, stream_started)."""
    if not check_gateway_alive(paths):
        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
    history_ctx = build_history_context()
    turn_inject = (
        list(job.inject) + [_CHAT_RESPONSE_STYLE_INJECT] + ([history_ctx] if history_ctx else [])
    )
    next_message_id = len(assistant_outputs) + 1
    on_chunk, stream_started_ref = _make_chunk_handler(agent.config.agent_name, next_message_id)
    request_id, chunk_path, response_path = submit_chat_request(
        paths,
        content=ChatRequestContent(
            prompt=job.user,
            inject=turn_inject,
            prompt_files=job.prompt_files,
            save=not args.no_save,
            show_prompt=job.show_prompt,
        ),
    )
    response_text = ""
    for chunk in poll_gateway_chunks(chunk_path, response_path, on_chunk=on_chunk):
        if chunk:
            response_text += chunk
    if not stream_started_ref[0]:
        _render_assistant_response(response_text, assistant_outputs, agent.config.agent_name)
    return response_text, stream_started_ref[0]


def _fallback_local_handle(
    job,
    agent,
    args,
    assistant_outputs: list[str],
    build_history_context: Callable[[], str],
) -> tuple[str, bool]:
    """Handle local-mode job. Returns (response_text, stream_started)."""
    history_ctx = build_history_context()
    turn_inject = (
        list(job.inject) + [_CHAT_RESPONSE_STYLE_INJECT] + ([history_ctx] if history_ctx else [])
    )
    next_message_id = len(assistant_outputs) + 1
    on_chunk, stream_started_ref = _make_chunk_handler(agent.config.agent_name, next_message_id)
    result = agent.run(
        job.user,
        inject=turn_inject,
        prompt_files=job.prompt_files,
        save=not args.no_save,
        source="chat",
        resume_context=resume_context_override(args),
        recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
        on_chunk=on_chunk,
    )
    agent_response_text = result.response
    if not stream_started_ref[0]:
        _render_assistant_response(agent_response_text, assistant_outputs, agent.config.agent_name)
    return agent_response_text, stream_started_ref[0]