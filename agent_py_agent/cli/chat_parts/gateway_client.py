
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


@dataclass
class ChatRequestContent:
    prompt: str
    inject: list[str]
    prompt_files: list[str]
    save: bool
    show_prompt: bool
    resume_context: object


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


def poll_gateway_chunks(
    chunk_path: Path,
    response_path: Path,
    deadline: float,
    on_chunk,  # callable(str) -> None
    *,
    chunks_printed_ref: list[int],
) -> dict:
    chunks_printed = chunks_printed_ref[0]
    response = {}
    while time.time() <= deadline:
        chunks_printed = _poll_chunk_file(chunk_path, on_chunk, chunks_printed)
        response = read_json_file(response_path)
        if response:
            chunks_printed = _poll_chunk_file(chunk_path, on_chunk, chunks_printed)
            break
        time.sleep(0.1)
    chunks_printed_ref[0] = chunks_printed
    return response


def _poll_chunk_file(chunk_path: Path, on_chunk: callable, chunks_printed: int) -> int:
    if not chunk_path.exists():
        return chunks_printed
    try:
        lines = chunk_path.read_text(encoding="utf-8").splitlines()
        for cline in lines[chunks_printed:]:
            chunks_printed += _emit_chunk_line(cline, on_chunk)
    except (OSError, json.JSONDecodeError):
        pass
    return chunks_printed


def _emit_chunk_line(cline: str, on_chunk: callable) -> int:
    if not cline.strip():
        return 0
    cobj = json.loads(cline)
    chunk_text = cobj.get("text", "")
    if chunk_text:
        visible = on_chunk(chunk_text)
        return 0 if visible is False else 1
    return 0


def check_gateway_alive(paths) -> bool:
    _, alive = gateway_running(paths)
    return alive


def format_gateway_timing(
    request_id: str,
    elapsed: float,
    response: dict,
    use_gateway: bool,
    agent_name: str,
) -> str:
    if use_gateway:
        return (
            f"[耗时 {elapsed:.2f}s; "
            f"工具轮数 {response.get('tool_rounds', 0)}; "
            f"prompt_tokens~{response.get('prompt_token_estimate', 0)}; "
            f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]"
        )
    else:
        return (
            f"[耗时 {elapsed:.2f}s; 工具轮数 {response.get('tool_rounds', 0)}; "
            f"prompt_tokens~{response.get('prompt_token_estimate', 0)}; "
            f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]"
        )


__all__ = [
    "ChatRequestContent",
    "check_gateway_alive",
    "format_gateway_timing",
    "poll_gateway_chunks",
    "submit_chat_request",
]
