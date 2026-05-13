# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

from .renderer import GREEN, strip_ansi, style_text


# LLM: _append_stream_text 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _append_stream_text(
    chunk: str,
    stream_buf_ref: list,
    stream_visible_text_ref: list | None = None,
) -> bool:
    if not chunk:
        return False
    from .rendering import _write_stream_text

    visible = strip_ansi(style_text(chunk, GREEN))
    _write_stream_text(visible)
    if stream_visible_text_ref is not None and visible.strip():
        stream_visible_text_ref[0] += visible
    stream_buf_ref[0] = "" if chunk.endswith("\n") else "\n"
    return bool(visible.strip())


# LLM: _emit_stream_line 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _emit_stream_line(text: str) -> None:
    from .rendering import _cprint

    _cprint(style_text(text, GREEN))


# LLM: _flush_stream_buf 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _flush_stream_buf(stream_buf_ref: list) -> None:
    buf = stream_buf_ref[0]
    if buf:
        from .rendering import _write_stream_text

        _write_stream_text(buf)
        stream_buf_ref[0] = ""


# LLM: _set_thinking_line 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _set_thinking_line(text: str, thinking_line_ref: list) -> None:
    import re

    if not text:
        thinking_line_ref[0] = ""
        return
    cleaned = text.replace("思考中:", "").strip()
    cleaned = re.sub(r"\s+\d+\.\d+s$", "", cleaned)
    thinking_line_ref[0] = cleaned


# LLM: _update_response_state 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _update_response_state(
    response: dict,
    state_lock,
    last_token_estimate_ref: list,
) -> str:
    agent_response_text = response.get("response", "")
    with state_lock:
        last_token_estimate_ref[0] = response.get("prompt_token_estimate", 0)
    return agent_response_text


# LLM: _maybe_record_response 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _maybe_record_response(
    text: str,
    stream_has_visible_text: bool,
    assistant_outputs: list[str],
    agent,
) -> bool:
    if text and (not stream_has_visible_text) and text.strip():
        from .fallback_ui import AssistantResponseRenderRequest, _render_assistant_response

        _render_assistant_response(
            AssistantResponseRenderRequest(text=text, assistant_outputs=assistant_outputs, agent_name=agent.config.agent_name)
        )
        return True
    return False


# LLM: resume_context_override 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def resume_context_override(args) -> str | None:
    if hasattr(args, "resume_context"):
        return args.resume_context
    return None
