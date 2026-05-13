# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。

from __future__ import annotations

"""small UI helpers for fallback chat mode.

给人看的解释：
这里只负责输入、展开历史回复和流式文本显示，业务执行仍在 fallback.py。
"""

import sys
import threading
from dataclasses import dataclass

from .fallback_state import FALLBACK_CHAT_PROMPT
from .input_loop import parse_expand_target
from .rendering import GRAY, GREEN, _cprint, collapse_response_text, style_text


# LLM: AssistantResponseRenderRequest bundles response-render settings for fallback and TUI workers.
# 类用途: 打包一次助手回复渲染需要的正文、输出列表、名称和折叠配置。
@dataclass(frozen=True)
class AssistantResponseRenderRequest:
    text: str
    assistant_outputs: list[str]
    agent_name: str
    preview_lines: int = 12
    preview_chars: int = 900


# LLM: _make_chunk_handler 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 构造下游调用需要的参数包、状态对象或命令对象。
def _make_chunk_handler(agent_name: str, next_message_id: int, *, preview_chars: int = 900):
    stream_started_ref = [False]
    stream_visible_chars_ref = [0]
    stream_truncated_ref = [False]
    preview_chars = max(0, int(preview_chars or 0))

    # LLM: on_chunk 属于chat CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def on_chunk(chunk: str) -> None:
        if not stream_started_ref[0]:
            sys.stdout.write(f"{style_text(f'{agent_name}#{next_message_id}>', GREEN)} ")
            sys.stdout.flush()
            stream_started_ref[0] = True
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

    return on_chunk, stream_started_ref


# LLM: _render_assistant_response 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
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


# LLM: _handle_expand_command 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
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


# LLM: _read_user_input 属于chat CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 读取文件、索引或配置，并转换成后续逻辑可直接使用的数据。
def _read_user_input(state_lock: threading.Lock, fallback_waiting_for_input_ref: list) -> str:
    if sys.stdin.isatty():
        print(FALLBACK_CHAT_PROMPT, end="", flush=True)
        fallback_waiting_for_input_ref[0] = True
        try:
            return input().strip()
        finally:
            fallback_waiting_for_input_ref[0] = False
    return input(FALLBACK_CHAT_PROMPT).strip()
