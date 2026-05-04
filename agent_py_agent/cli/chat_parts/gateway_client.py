"""LLM: gateway request/response streaming helpers for chat mode.

给人看的解释：
gateway 客户端的请求提交、chunk 读取、响应轮询都集中在这里，
让 TUI 和 fallback 两个循环都复用同一套 gateway 交互逻辑。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ...agent.gateway import (
    gateway_chunk_path,
    gateway_paths,
    gateway_running,
    read_json_file,
    render_gateway_status,
    submit_gateway_ask,
)


def submit_chat_request(
    paths,
    prompt: str,
    inject: list[str],
    prompt_files: list[str],
    save: bool,
    show_prompt: bool,
    resume_context,
    agent,
) -> tuple[str, Path, Path]:
    """Submit a gateway chat request and return (request_id, chunk_path, response_path)."""
    request_id, _, response_path = submit_gateway_ask(
        paths,
        prompt=prompt,
        inject=inject,
        prompt_files=prompt_files,
        save=save,
        include_prompt=show_prompt,
        resume_context=resume_context,
        agent=agent,
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
    """Poll gateway chunk and response files until deadline.

    Returns the final response dict or empty dict on timeout.
    """
    chunks_printed = chunks_printed_ref[0]
    response = {}
    while time.time() <= deadline:
        chunks_printed = _poll_chunk_file(chunk_path, on_chunk, chunks_printed)
        response = read_json_file(response_path)
        if response:
            break
        time.sleep(0.1)
    chunks_printed_ref[0] = chunks_printed
    return response


def _poll_chunk_file(chunk_path: Path, on_chunk: callable, chunks_printed: int) -> int:
    """Poll one chunk file, calling on_chunk for new lines. Returns updated count."""
    if not chunk_path.exists():
        return chunks_printed
    try:
        lines = chunk_path.read_text(encoding="utf-8").splitlines()
        for cline in lines[chunks_printed:]:
            if not cline.strip():
                continue
            cobj = json.loads(cline)
            chunk_text = cobj.get("text", "")
            if chunk_text:
                on_chunk(chunk_text)
            chunks_printed += 1
    except (OSError, json.JSONDecodeError):
        pass
    return chunks_printed


def check_gateway_alive(paths) -> bool:
    """Check if gateway is running and alive."""
    _, alive = gateway_running(paths)
    return alive


def format_gateway_timing(
    request_id: str,
    elapsed: float,
    response: dict,
    use_gateway: bool,
    agent_name: str,
) -> str:
    """Format gateway timing summary string."""
    if use_gateway:
        return (
            f"[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
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
    "check_gateway_alive",
    "format_gateway_timing",
    "poll_gateway_chunks",
    "submit_chat_request",
]