# LLM: CLI chat UI helper; keep transcript, fallback, and TUI contracts stable for interactive sessions.
# 模块用途: 支撑命令行聊天界面的渲染、输入、历史记录或后台工作线程。


from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ...agent.gateway import (
    GatewayAskParams,
    gateway_chunk_path,
    gateway_paths,
    gateway_running,
    read_json_file,
    render_gateway_status,
    submit_gateway_ask,
)


# LLM: ChatRequestContent 是gateway CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
@dataclass
class ChatRequestContent:
    prompt: str
    inject: list[str]
    prompt_files: list[str]
    save: bool
    show_prompt: bool
    resume_context: object


# LLM: GatewayChunkPollRequest 是gateway CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class GatewayChunkPollRequest:
    chunk_path: Path
    response_path: Path
    deadline: float
    on_chunk: object
    chunks_printed_ref: list[int]
    visible_chunks_ref: list[int] | None = None


# LLM: GatewayTimingContext 是gateway CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass(frozen=True)
class GatewayTimingContext:
    request_id: str
    elapsed: float
    response: dict
    use_gateway: bool


# LLM: submit_chat_request 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def submit_chat_request(
    paths,
    content: ChatRequestContent,
    agent,
) -> tuple[str, Path, Path]:
    request_id, _, response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt=content.prompt,
            inject=content.inject,
            prompt_files=content.prompt_files,
            save=content.save,
            include_prompt=content.show_prompt,
            resume_context=content.resume_context,
            agent=agent,
        ),
    )
    chunk_path = gateway_chunk_path(paths, request_id)
    return request_id, chunk_path, response_path


# LLM: poll_gateway_chunks 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 协调 gateway 请求、进程状态、worker 或本地文件之间的流转。
def poll_gateway_chunks(request: GatewayChunkPollRequest) -> dict:
    chunks_printed = request.chunks_printed_ref[0]
    visible_chunks = request.visible_chunks_ref[0] if request.visible_chunks_ref else 0
    response = {}
    while time.time() <= request.deadline:
        chunks_printed, visible_chunks = _poll_chunk_file(
            request.chunk_path, request.on_chunk, chunks_printed, visible_chunks
        )
        response = read_json_file(request.response_path)
        if response:
            chunks_printed, visible_chunks = _poll_chunk_file(
                request.chunk_path, request.on_chunk, chunks_printed, visible_chunks
            )
            break
        time.sleep(0.1)
    request.chunks_printed_ref[0] = chunks_printed
    if request.visible_chunks_ref is not None:
        request.visible_chunks_ref[0] = visible_chunks
    return response


# LLM: _poll_chunk_file 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _poll_chunk_file(
    chunk_path: Path,
    on_chunk: callable,
    chunks_printed: int,
    visible_chunks: int,
) -> tuple[int, int]:
    if not chunk_path.exists():
        return chunks_printed, visible_chunks
    try:
        lines = chunk_path.read_text(encoding="utf-8").splitlines()
        for cline in lines[chunks_printed:]:
            consumed, visible = _emit_chunk_line(cline, on_chunk)
            chunks_printed += consumed
            visible_chunks += visible
    except (OSError, json.JSONDecodeError):
        pass
    return chunks_printed, visible_chunks


# LLM: _emit_chunk_line 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _emit_chunk_line(cline: str, on_chunk: callable) -> tuple[int, int]:
    if not cline.strip():
        return 0, 0
    cobj = json.loads(cline)
    chunk_text = cobj.get("text", "")
    if chunk_text:
        visible = on_chunk(chunk_text)
        return 1, 1 if visible is True else 0
    return 1, 0


# LLM: check_gateway_alive 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def check_gateway_alive(paths) -> bool:
    _, alive = gateway_running(paths)
    return alive


# LLM: format_gateway_timing 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def format_gateway_timing(ctx: GatewayTimingContext) -> str:
    if ctx.use_gateway:
        return (
            f"[耗时 {ctx.elapsed:.2f}s; "
            f"工具轮数 {ctx.response.get('tool_rounds', 0)}; "
            f"prompt_tokens~{ctx.response.get('prompt_token_estimate', 0)}; "
            f"resume_context={1 if ctx.response.get('memory_resume_context_injected') else 0}]"
        )
    return (
        f"[耗时 {ctx.elapsed:.2f}s; 工具轮数 {ctx.response.get('tool_rounds', 0)}; "
        f"prompt_tokens~{ctx.response.get('prompt_token_estimate', 0)}; "
        f"resume_context={1 if ctx.response.get('memory_resume_context_injected') else 0}]"
    )


__all__ = [
    "ChatRequestContent",
    "GatewayChunkPollRequest",
    "GatewayTimingContext",
    "check_gateway_alive",
    "format_gateway_timing",
    "poll_gateway_chunks",
    "submit_chat_request",
]
