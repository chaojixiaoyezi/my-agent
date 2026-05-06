from __future__ import annotations

import sys

from .renderer import GREEN, style_text


def _append_stream_text(chunk: str, stream_buf_ref: list) -> None:
    if not chunk:
        return
    sys.stdout.write(style_text(chunk, GREEN))
    sys.stdout.flush()
    stream_buf_ref[0] = "" if chunk.endswith("\n") else "\n"


def _emit_stream_line(text: str) -> None:
    from .rendering import _cprint

    _cprint(style_text(text, GREEN))


def _flush_stream_buf(stream_buf_ref: list) -> None:
    buf = stream_buf_ref[0]
    if buf:
        sys.stdout.write(buf)
        sys.stdout.flush()
        stream_buf_ref[0] = ""


def _set_thinking_line(text: str, thinking_line_ref: list) -> None:
    import re

    if not text:
        thinking_line_ref[0] = ""
        return
    cleaned = text.replace("思考中:", "").strip()
    cleaned = re.sub(r"\s+\d+\.\d+s$", "", cleaned)
    thinking_line_ref[0] = cleaned


def _update_response_state(
    response: dict,
    state_lock,
    last_token_estimate_ref: list,
) -> str:
    agent_response_text = response.get("response", "")
    with state_lock:
        last_token_estimate_ref[0] = response.get("prompt_token_estimate", 0)
    return agent_response_text


def _maybe_record_response(
    text: str,
    stream_has_visible_text: bool,
    assistant_outputs: list[str],
    agent,
) -> bool:
    if text and (not stream_has_visible_text) and text.strip():
        from .fallback_ui import _render_assistant_response

        _render_assistant_response(text, assistant_outputs, agent.config.agent_name)
        return True
    return False


def resume_context_override(args) -> str | None:
    if hasattr(args, "resume_context"):
        return args.resume_context
    return None
