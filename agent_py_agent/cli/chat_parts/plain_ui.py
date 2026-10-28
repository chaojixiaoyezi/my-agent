
# LLM: plain显示只接公开完整正文或原Gateway公开chunk；local候选不得直接stdout，提交复用同源typed安全投影，不授业务状态。
# 模块用途: 普通终端的安全正文、折叠与展开；直接使用slash解析，内部历史与执行仍由原worker负责。
from __future__ import annotations

"""small UI helpers for plain chat mode.

给人看的解释：
这里只负责输入、展开历史回复和流式文本显示，业务执行仍在 plain.py。
"""

import sys
import threading
from dataclasses import dataclass

from ...agent.gateway_parts.stream_events import committed_model_events, public_model_text
from .plain_state import PLAIN_CHAT_PROMPT
from .renderer import strip_ansi
from .rendering import GRAY, MAGENTA, _cprint, collapse_response_text, style_text
from .slash_commands import parse_expand_target


@dataclass(frozen=True)
class AssistantResponseRenderRequest:
    text: str
    assistant_outputs: list[str]
    agent_name: str
    preview_lines: int = 12
    preview_chars: int = 900


# LLM: Gateway兼容回调只接已公开chunk；local显式committed-only暂不显示候选，原同call提交才显示过程，终答沿原完整renderer。
# 函数用途: 建立一次普通聊天的显示回调，不发模型请求，工具状态与模型正文保持结构化来源。
def _make_chunk_handler(agent_name: str, next_message_id: int, *, preview_chars: int = 900,
                        committed_model_only: bool = False):
    stream_started_ref = [False]
    stream_visible_chars_ref = [0]
    stream_truncated_ref = [False]
    preview_chars = max(0, int(preview_chars or 0))
    committed_calls = set()

    # LLM: local候选不进stdout；Gateway输入已在服务端完整公开净化，保原流式折叠而不逐chunk重新拆安全上下文。
    # 函数用途: 渲染已公开Gateway流，或等待local整次响应确认。
    def on_chunk(chunk: str) -> bool:
        if committed_model_only:
            return False
        if not stream_started_ref[0]:
            sys.stdout.write(f"{style_text('⏺', MAGENTA)} ")
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

    # LLM: 只读已sealed response的typed投影，按真实call去重；不按正文猜来源，不打印终答以免与原完整renderer重复。
    # 函数用途: 在local真实工具边界显示已验过程说明，支持流式和非流式后端。
    def commit_model_response(payload: dict) -> None:
        events = committed_model_events(payload)
        call_id = (payload.get('source') or {}).get('call_id')
        if not events or call_id in committed_calls:
            return
        committed_calls.add(call_id)
        for event in events:
            if event['kind'] == 'assistant_commentary':
                _cprint(f"{style_text('⏺', MAGENTA)} {event['text']}")

    # LLM: 仅接受原结构化progress对应的宿主legacy显示，不把工具状态当模型正文或最终回复。
    # 函数用途: local保留原工具进展上屏，不消耗完整终答的显示标记。
    def write_progress(_event: dict, legacy_text: str) -> None:
        if legacy_text:
            sys.stdout.write(legacy_text)
            sys.stdout.flush()

    if committed_model_only:
        on_chunk.commit_model_response = commit_model_response
        on_chunk.write_progress = write_progress
    return on_chunk, stream_started_ref


def _is_progress_chunk(chunk: str) -> bool:
    text = strip_ansi(chunk).strip()
    if not text:
        return False
    return text.startswith(("[工具]", "[TOOL", "[MAIN_AGENT_", "工具 ", "tool "))


def _normalized_progress_chunk(chunk: str) -> str:
    text = chunk if chunk.startswith("\n") else "\n" + chunk
    return text if text.endswith("\n") else text + "\n"


# LLM: 内部response完整净化后才折叠和保存expand列表；空公开投影不伪造回复，不改变内部任务状态。
# 函数用途: 显示一次安全终答并保存可展开公开正文。
def _render_assistant_response(request: AssistantResponseRenderRequest) -> None:
    text = public_model_text(request.text, max_chars=0, delivery_channel="", identifiers=())
    if not text:
        return
    preview, collapsed = collapse_response_text(
        text,
        preview_lines=request.preview_lines,
        preview_chars=request.preview_chars,
    )
    request.assistant_outputs.append(text)
    message_id = len(request.assistant_outputs)
    if collapsed:
        _cprint(f"{style_text('⏺', MAGENTA)} {preview}")
        _cprint(
            style_text(f"[回复较长，已自动折叠。输入 /expand {message_id} 或 /expand last 查看全文。]", GRAY)
        )
        return
    _cprint(f"{style_text('⏺', MAGENTA)} {text}")


# LLM: 展开旧进程内列表也必须在原公开门净化，不能因此前记录来自旧writer而直接恢复未验原文。
# 函数用途: 按显式命令显示安全的完整历史回复，不改变模型历史或执行状态。
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
    print(public_model_text(assistant_outputs[index - 1], max_chars=0, delivery_channel="", identifiers=()))
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
