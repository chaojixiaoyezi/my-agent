
from __future__ import annotations

"""small UI helpers for plain chat mode.

给人看的解释：
这里只负责输入、展开历史回复和流式文本显示，业务执行仍在 plain.py。
"""

import sys
import threading
from dataclasses import dataclass

from .input_loop import parse_expand_target
from .plain_state import PLAIN_CHAT_PROMPT
from .renderer import strip_ansi
from .rendering import GRAY, GREEN, _cprint, collapse_response_text, style_text


@dataclass(frozen=True)
class AssistantResponseRenderRequest:
    text: str
    assistant_outputs: list[str]
    agent_name: str
    preview_lines: int = 12
    preview_chars: int = 900


def _make_chunk_handler(agent_name: str, next_message_id: int, *, preview_chars: int = 900):
    stream_started_ref = [False]
    stream_visible_chars_ref = [0]
    stream_truncated_ref = [False]
    preview_chars = max(0, int(preview_chars or 0))

    def on_chunk(chunk: str) -> bool:
        if not stream_started_ref[0]:
            sys.stdout.write(f"{style_text(f'{agent_name}#{next_message_id}>', GREEN)} ")
            sys.stdout.flush()
            stream_started_ref[0] = True
        if stream_truncated_ref[0] and _is_progress_chunk(chunk):
            sys.stdout.write(_normalized_progress_chunk(chunk))
            sys.stdout.flush()
            return True
        remaining = max(0, preview_chars - stream_visible_chars_ref[0])
        if remaining > 0:
            visible = chunk[:remaining]
            sys.stdout.write(visible)
            sys.stdout.flush()
            stream_visible_chars_ref[0] += len(visible)
        if remaining < len(chunk) and not stream_truncated_ref[0]:
            sys.stdout.write(
                style_text("[回复较长，后续内容已折叠。完成后可用 /expand last 查看全文。]", GRAY)
            )
            sys.stdout.flush()
            stream_truncated_ref[0] = True
            return True
        return remaining > 0

    return on_chunk, stream_started_ref


def _is_progress_chunk(chunk: str) -> bool:
    text = strip_ansi(chunk).strip()
    if not text:
        return False
    return text.startswith(("[工具]", "[TOOL", "[MAIN_AGENT_", "工具 ", "tool "))


def _normalized_progress_chunk(chunk: str) -> str:
    text = chunk if chunk.startswith("\n") else "\n" + chunk
    return text if text.endswith("\n") else text + "\n"


def _render_assistant_response(request: AssistantResponseRenderRequest) -> None:
    preview, collapsed = collapse_response_text(
        request.text,
        preview_lines=request.preview_lines,
        preview_chars=request.preview_chars,
    )
    request.assistant_outputs.append(request.text)
    message_id = len(request.assistant_outputs)
    if collapsed:
        _cprint(f"{style_text(f'{request.agent_name}#{message_id}>', GREEN)} {preview}")
        _cprint(
            style_text(f"[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]", GRAY)
        )
        return
    _cprint(f"{style_text(f'{request.agent_name}#{message_id}>', GREEN)} {request.text}")


def _handle_expand_command(raw: str, assistant_outputs: list[str]) -> None:
    target = parse_expand_target(raw)
    if target is None:
        print("用法: /expand [last|编号]")
        return
    if not assistant_outputs:
        print("当前没有可展开的助手回复。")
        return
    if target == "last":
        index = len(assistant_outputs)
    else:
        index = int(target)
    if index < 1 or index > len(assistant_outputs):
        print(f"没有编号为 {index} 的助手回复。当前共有 {len(assistant_outputs)} 条。")
        return
    print(f"===== ASSISTANT RESPONSE #{index} =====")
    print(assistant_outputs[index - 1])
    print("===== END RESPONSE =====")


def _read_user_input(state_lock: threading.Lock, plain_waiting_for_input_ref: list) -> str:
    if sys.stdin.isatty():
        print(PLAIN_CHAT_PROMPT, end="", flush=True)
        plain_waiting_for_input_ref[0] = True
        try:
            return input().strip()
        finally:
            plain_waiting_for_input_ref[0] = False
    return input(PLAIN_CHAT_PROMPT).strip()
