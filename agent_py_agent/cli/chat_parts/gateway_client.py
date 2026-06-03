

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ...agent.gateway import (
    GatewayAskParams,
    gateway_chunk_path,
    gateway_paths,
    gateway_running,
    render_gateway_status,
    submit_gateway_ask,
)
from ...agent.gateway_parts.response_renderer import read_gateway_response_file


@dataclass
class ChatRequestContent:
    prompt: str
    inject: list[str]
    prompt_files: list[str]
    save: bool
    show_prompt: bool
    resume_context: object


@dataclass(frozen=True)
class GatewayChunkPollRequest:
    chunk_path: Path
    response_path: Path
    deadline: float
    on_chunk: object
    chunks_printed_ref: list[int]
    visible_chunks_ref: list[int] | None = None


@dataclass(frozen=True)
class GatewayTimingContext:
    request_id: str
    elapsed: float
    response: dict
    use_gateway: bool


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


def poll_gateway_chunks(request: GatewayChunkPollRequest) -> dict:
    chunks_printed = request.chunks_printed_ref[0]
    visible_chunks = request.visible_chunks_ref[0] if request.visible_chunks_ref else 0
    response = {}
    while time.time() <= request.deadline:
        chunks_printed, visible_chunks = _poll_chunk_file(
            request.chunk_path, request.on_chunk, chunks_printed, visible_chunks
        )
        response = read_gateway_response_file(
            request.response_path,
            context="gateway.chat.response.read",
        )
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
    except OSError as exc:
        print(f"gateway chat chunk load_error path={chunk_path} message={exc}", file=sys.stderr)
        return chunks_printed, visible_chunks
    for cline in lines[chunks_printed:]:
        consumed, visible = _emit_chunk_line(cline, on_chunk)
        chunks_printed += consumed
        visible_chunks += visible
    return chunks_printed, visible_chunks


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
