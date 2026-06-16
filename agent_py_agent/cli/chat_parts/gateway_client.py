

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ...agent.gateway_parts import (
    GatewayAskParams,
    gateway_chunk_path,
    gateway_chunk_path_candidates,
    gateway_paths,
    gateway_running,
    render_gateway_status,
    submit_gateway_ask,
)
from ...agent.gateway_parts.response_renderer import (
    GatewayResponsePollState,
    current_context_token_estimate,
    read_gateway_response_file,
    read_gateway_response_file_when_ready,
)


@dataclass
class ChatRequestContent:
    prompt: str
    inject: list[str]
    prompt_files: list[str]
    save: bool
    show_prompt: bool
    resume_context: object
    chat_session_id: str = ""


@dataclass(frozen=True)
class GatewayChunkPollRequest:
    chunk_path: Path
    response_path: Path
    deadline: float
    on_chunk: object
    chunks_printed_ref: list[int]
    visible_chunks_ref: list[int] | None = None
    chunk_offset_ref: list[int] | None = None


@dataclass(frozen=True)
class GatewayTimingContext:
    request_id: str
    elapsed: float
    response: dict
    use_gateway: bool


@dataclass(frozen=True)
class ChunkFilePollRequest:
    chunk_path: Path
    on_chunk: object
    chunks_printed: int
    visible_chunks: int
    chunk_offset: int


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
            chat_session_id=content.chat_session_id,
            agent=agent,
        ),
    )
    chunk_path = gateway_chunk_path(paths, request_id)
    return request_id, chunk_path, response_path


def poll_gateway_chunks(request: GatewayChunkPollRequest) -> dict:
    chunks_printed = request.chunks_printed_ref[0]
    visible_chunks = request.visible_chunks_ref[0] if request.visible_chunks_ref else 0
    chunk_offset = request.chunk_offset_ref[0] if request.chunk_offset_ref else 0
    response = {}
    response_poll_state = GatewayResponsePollState()
    while time.time() <= request.deadline:
        chunks_printed, visible_chunks, chunk_offset = _poll_chunk_file(
            ChunkFilePollRequest(request.chunk_path, request.on_chunk, chunks_printed, visible_chunks, chunk_offset)
        )
        response = read_gateway_response_file_when_ready(
            request.response_path,
            state=response_poll_state,
            context="gateway.chat.response.read",
        )
        if response:
            chunks_printed, visible_chunks, chunk_offset = _poll_chunk_file(
                ChunkFilePollRequest(request.chunk_path, request.on_chunk, chunks_printed, visible_chunks, chunk_offset)
            )
            break
        time.sleep(0.1)
    request.chunks_printed_ref[0] = chunks_printed
    if request.visible_chunks_ref is not None:
        request.visible_chunks_ref[0] = visible_chunks
    if request.chunk_offset_ref is not None:
        request.chunk_offset_ref[0] = chunk_offset
    return response


# 单次读取上限:流式 chunk 文件可能很大,整体 f.read() 无上限会 MemoryError。
_MAX_CHUNK_READ_BYTES = 8 * 1024 * 1024


def _poll_chunk_file(request: ChunkFilePollRequest) -> tuple[int, int, int]:
    chunks_printed = request.chunks_printed
    visible_chunks = request.visible_chunks
    chunk_offset = request.chunk_offset
    readable_chunk_path = _readable_chunk_path(request.chunk_path)
    if readable_chunk_path is None:
        return chunks_printed, visible_chunks, chunk_offset
    try:
        with open(readable_chunk_path, encoding="utf-8") as f:
            f.seek(max(0, chunk_offset))
            # 单次读取上限,防止超大 chunk 文件整体读入触发 MemoryError;
            # 剩余部分下一拍轮询继续(chunk_offset 已推进)。
            data = f.read(_MAX_CHUNK_READ_BYTES)
            chunk_offset = f.tell()
    except OSError as exc:
        print(f"gateway chat chunk load_error path={readable_chunk_path} message={exc}", file=sys.stderr)
        return chunks_printed, visible_chunks, 0
    for cline in data.splitlines():
        consumed, visible = _emit_chunk_line(cline, request.on_chunk)
        chunks_printed += consumed
        visible_chunks += visible
    return chunks_printed, visible_chunks, chunk_offset


def _readable_chunk_path(chunk_path: Path) -> Path | None:
    for candidate in gateway_chunk_path_candidates(chunk_path):
        if candidate.exists():
            return candidate
    return None


def _emit_chunk_line(cline: str, on_chunk: callable) -> tuple[int, int]:
    if not cline.strip():
        return 1, 0
    try:
        cobj = json.loads(cline)
    except json.JSONDecodeError as exc:
        print(f"gateway chat chunk load_error category=json_decode message={exc}", file=sys.stderr)
        return 1, 0
    if not isinstance(cobj, dict):
        print("gateway chat chunk load_error category=non_object_root", file=sys.stderr)
        return 1, 0
    chunk_text = cobj.get("text", "")
    if chunk_text:
        visible = on_chunk(chunk_text)
        return 1, 1 if visible is True else 0
    return 1, 0


def check_gateway_alive(paths) -> bool:
    _, alive = gateway_running(paths)
    return alive


def format_gateway_timing(ctx: GatewayTimingContext) -> str:
    ctx_tokens = current_context_token_estimate(ctx.response)
    if ctx.use_gateway:
        return (
            f"[耗时 {ctx.elapsed:.2f}s; "
            f"工具轮数 {ctx.response.get('tool_rounds', 0)}; "
            f"ctx_tokens~{ctx_tokens}; "
            f"prompt_tokens~{ctx.response.get('prompt_token_estimate', 0)}; "
            f"resume_context={1 if ctx.response.get('memory_resume_context_injected') else 0}]"
        )
    return (
        f"[耗时 {ctx.elapsed:.2f}s; 工具轮数 {ctx.response.get('tool_rounds', 0)}; "
        f"ctx_tokens~{ctx_tokens}; "
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
