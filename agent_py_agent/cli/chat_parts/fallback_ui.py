from __future__ import annotations

"""LLM: small UI helpers for fallback chat mode.

给人看的解释：
这里只负责输入、展开历史回复和流式文本显示，业务执行仍在 fallback.py。
"""

import sys
import threading

from .fallback_state import FALLBACK_CHAT_PROMPT
from .input_loop import parse_expand_target
from .rendering import GRAY, GREEN, RESET, collapse_response_text


def _make_chunk_handler(agent_name: str, next_message_id: int):
    stream_started_ref = [False]
    stream_visible_chars_ref = [0]
    stream_truncated_ref = [False]

    def on_chunk(chunk: str) -> None:
        if not stream_started_ref[0]:
            sys.stdout.write(f"{GREEN}{agent_name}#{next_message_id}>{RESET} ")
            sys.stdout.flush()
            stream_started_ref[0] = True
        remaining = max(0, 900 - stream_visible_chars_ref[0])
        if remaining > 0:
            visible = chunk[:remaining]
            sys.stdout.write(visible)
            sys.stdout.flush()
            stream_visible_chars_ref[0] += len(visible)
        if remaining < len(chunk) and not stream_truncated_ref[0]:
            sys.stdout.write(
                f"{GRAY}[回复较长，后续内容已折叠。完成后可用 /expand last 查看全文。]{RESET}"
            )
            sys.stdout.flush()
            stream_truncated_ref[0] = True

    return on_chunk, stream_started_ref


def _render_assistant_response(text: str, assistant_outputs: list[str], agent_name: str) -> None:
    preview, collapsed = collapse_response_text(text)
    assistant_outputs.append(text)
    message_id = len(assistant_outputs)
    if collapsed:
        print(f"{GREEN}{agent_name}#{message_id}>{RESET} {preview}")
        print(
            f"{GRAY}[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]{RESET}"
        )
        return
    print(f"{GREEN}{agent_name}#{message_id}>{RESET} {text}")


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


def _read_user_input(state_lock: threading.Lock, fallback_waiting_for_input_ref: list) -> str:
    if sys.stdin.isatty():
        print(FALLBACK_CHAT_PROMPT, end="", flush=True)
        fallback_waiting_for_input_ref[0] = True
        try:
            return input().strip()
        finally:
            fallback_waiting_for_input_ref[0] = False
    return input(FALLBACK_CHAT_PROMPT).strip()


