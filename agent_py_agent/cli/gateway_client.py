
from __future__ import annotations

"""implements gateway command dispatch, default startup, ask/result client commands.

给人看的解释：
这个文件是 gateway 的'客户端侧'：确保后台进程启动、投递 ask 请求、读取某个请求结果。
真正的进程生命周期在 gateway_process.py。
响应文件轮询走 gateway response_renderer 的统一状态读取，避免 chat/gateway 两套读响应逻辑漂移。
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.gateway_parts import (
    GatewayAskParams,
    gateway_chunk_path,
    gateway_chunk_path_candidates,
    gateway_paths,
    gateway_running,
    log_gateway_payload,
    print_gateway_response,
    submit_gateway_ask,
    wait_for_gateway_response,
    wait_for_gateway_running,
)
from ..agent.gateway_parts.io import read_complete_utf8_rows
from ..agent.gateway_parts.response_renderer import (
    GatewayResponsePollState,
    project_gateway_stream_chunk,
    read_gateway_terminal_response_file,
    read_gateway_terminal_response_file_when_ready,
)
from .chat import cmd_chat
from .common import make_agent, resume_context_override
from .gateway_process import cmd_gateway_start
from .thinking_spinner import ThinkingSpinner


@dataclass
class GatewayAskContext:
    agent: object
    paths: object
    request_id: str
    request_path: object
    response_path: object
    timeout: float
    stream_output: bool = True


@dataclass(frozen=True)
class GatewayPollRequest:
    chunk_path: Path
    terminal_path: Path
    deadline: float
    spinner: ThinkingSpinner
    stream_output: bool


@dataclass(frozen=True)
class GatewayPollResult:
    payload: dict[str, Any]
    streamed_text: bool


@dataclass(frozen=True)
class GatewaySubmittedAsk:
    request_id: str
    request_path: object
    response_path: object


@dataclass
class GatewayStreamState:
    chunks_printed: int = 0
    chunk_offset: int = 0
    visible_chunks: int = 0


def cmd_gateway(args) -> int:

    print("请指定 gateway 子命令：start / supervisor-start / status / stop / restart / logs / ask / result / start-all。", file=sys.stderr)
    return 2


def ensure_gateway_started(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = gateway_running(paths)
    if alive:
        return 0
    start_args = argparse.Namespace(
        config=args.config,
        force=False,
        force_lock=False,
    )
    code = cmd_gateway_start(start_args)
    if code:
        return code
    deadline = time.time() + 10
    while time.time() < deadline:
        pid, alive = gateway_running(paths)
        if alive:
            return 0
        time.sleep(0.2)
    print("gateway 已尝试启动，但未能确认存活。请运行 my-agent gateway status 查看。", file=sys.stderr)
    return 2


def cmd_default(args) -> int:
    code = ensure_gateway_started(args)
    if code:
        return code
    code = _maybe_handle_active_work(args)
    if code:
        return code
    args.gateway = True
    args.gateway_timeout = None
    args.inject = None
    args.prompt_file = None
    args.memory_limit = None
    args.no_save = False
    args.app_scrollback = not bool(getattr(args, "plain", False))
    return cmd_chat(args)


def _maybe_handle_active_work(args) -> int | None:
    agent = make_agent(args)
    if not agent or not agent.config.auto_detect_work_on_startup:
        return None
    return _handle_active_work_prompt(agent)


def _handle_active_work_prompt(agent) -> int | None:
    from ..agent.startup_recovery import (
        detect_active_work,
        format_active_work_summary,
        has_active_work,
    )
    summary = detect_active_work(agent)
    if not has_active_work(summary):
        return None
    print("\n" + "=" * 60)
    print("进行中任务检测")
    print("=" * 60)
    print(format_active_work_summary(summary))
    print("=" * 60 + "\n")
    if summary.active_task_count <= 0:
        return None
    try:
        response = input("是否继续调度这些任务？[Y/n] ").strip().lower()
        if response and response not in {"y", "yes", ""}:
            print("已取消自动调度。")
            return 0
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 0
    return None


def _stream_chunk_lines(
    chunk_path: Path,
    spinner,
    state: GatewayStreamState | None = None,
) -> int:
    readable_chunk_path = _readable_chunk_path(chunk_path)
    if readable_chunk_path is None:
        return state.chunks_printed if state else 0
    try:
        data = _read_stream_chunk_data(readable_chunk_path, state)
    except OSError as exc:
        print(f"gateway stream chunk load_error path={readable_chunk_path} message={exc}", file=sys.stderr)
        return state.chunks_printed if state else 0
    chunks_printed = state.chunks_printed if state else 0
    lines = data.splitlines() if state is not None else data.splitlines()[chunks_printed:]
    for line in lines:
        consumed, visible = _write_stream_chunk_line(line, chunks_printed, spinner)
        chunks_printed += consumed
        if visible and state is not None:
            state.visible_chunks += 1
    if state is not None:
        state.chunks_printed = chunks_printed
    return chunks_printed


# 单次读取上限:流式 chunk 文件可能很大,整体 f.read() 无上限会 MemoryError;
# 按上限分块读,剩余部分下一拍轮询继续(state.chunk_offset 已推进)。
_MAX_CHUNK_READ_BYTES = 8 * 1024 * 1024


def _read_stream_chunk_data(readable_chunk_path: Path, state: GatewayStreamState | None) -> str:
    offset = state.chunk_offset if state is not None else 0
    rows, next_offset, decode_error = read_complete_utf8_rows(
        readable_chunk_path,
        offset,
        max_bytes=_MAX_CHUNK_READ_BYTES,
    )
    if decode_error:
        print("gateway stream chunk load_error category=utf8_decode", file=sys.stderr)
    if state is not None:
        state.chunk_offset = next_offset
    return "\n".join(rows)


def _readable_chunk_path(chunk_path: Path) -> Path | None:
    for candidate in gateway_chunk_path_candidates(chunk_path):
        if candidate.exists():
            return candidate
    return None


def _write_stream_chunk_line(line: str, chunks_printed: int, spinner) -> tuple[int, bool]:
    if not line.strip():
        return 1, False
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        print(
            "gateway stream chunk load_error "
            f"line={chunks_printed + 1} category=json_decode message={exc}",
            file=sys.stderr,
        )
        return 1, False
    if not isinstance(obj, dict):
        print(
            "gateway stream chunk load_error "
            f"line={chunks_printed + 1} category=non_object_root",
            file=sys.stderr,
        )
        return 1, False
    text, terminal_response_streamed = project_gateway_stream_chunk(obj)
    if text:
        spinner.stop()
    sys.stdout.write(text)
    sys.stdout.flush()
    return 1, bool(text) and terminal_response_streamed


def _flush_stream_chunks(request: GatewayPollRequest, state: GatewayStreamState) -> int:
    if not request.stream_output:
        return state.chunks_printed
    return _stream_chunk_lines(request.chunk_path, request.spinner, state)


# LLM: Local CLI polling shares the same canonical terminal authority as HTTP, TUI, and adapter;
# chunks are display-only and an orphan response projection must never finish the wait.
# 函数用途: 在输出流式进度的同时等待唯一终态归档。
def _wait_for_gateway_response(request: GatewayPollRequest) -> GatewayPollResult:
    stream_state = GatewayStreamState()
    response: dict[str, Any] = {}
    response_poll_state = GatewayResponsePollState()

    while time.time() <= request.deadline:
        _flush_stream_chunks(request, stream_state)
        response = read_gateway_terminal_response_file_when_ready(
            request.terminal_path,
            state=response_poll_state,
            request_id=request.terminal_path.stem,
            context="gateway.cli.terminal.read",
        )
        if response:
            _flush_stream_chunks(request, stream_state)
            break
        time.sleep(0.1)

    return GatewayPollResult(response, bool(stream_state.visible_chunks))


# LLM: The submitted response path remains a repairable projection; derive completion from the
# exact request id under requests/terminal instead.
# 函数用途: 为一次 ask 组装流路径、终态路径和超时上限并执行轮询。
def _poll_gateway_response(ctx: GatewayAskContext) -> GatewayPollResult:
    chunk_path = gateway_chunk_path(ctx.paths, ctx.request_id)
    spinner = ThinkingSpinner()
    if ctx.stream_output:
        spinner.start()
    deadline = time.time() + max(0.0, ctx.timeout)
    terminal_path = ctx.paths.terminal / f"{ctx.request_id}.json"
    result = _wait_for_gateway_response(
        GatewayPollRequest(
            chunk_path,
            terminal_path,
            deadline,
            spinner,
            ctx.stream_output,
        )
    )
    if ctx.stream_output:
        spinner.stop()
    return result


def _handle_gateway_timeout(ctx: GatewayAskContext) -> int:
    log_gateway_payload(
        ctx.agent,
        {
            "id": ctx.request_id,
            "kind": "ask",
            "status": "timeout",
            "ok": False,
            "error": f"timeout after {ctx.timeout}s",
            "created_at": 0,
            "ended_at": time.time(),
        },
        event_type="gateway_request_timeout",
        request_path=ctx.request_path,
        response_path=ctx.response_path,
    )
    print(f"gateway 请求等待超时: request_id={ctx.request_id} timeout={ctx.timeout}s", file=sys.stderr)
    print(f"response: {ctx.response_path}")
    return 2


def _submit_gateway_ask(args, agent, paths) -> GatewaySubmittedAsk:
    request_id, request_path, response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt=args.prompt,
            inject=args.inject or [],
            prompt_files=args.prompt_file or [],
            save=not args.no_save,
            include_prompt=bool(args.show_prompt),
            resume_context=resume_context_override(args),
            agent=agent,
        ),
    )
    return GatewaySubmittedAsk(request_id, request_path, response_path)


def _gateway_ask_context(args, agent, paths, submitted: GatewaySubmittedAsk) -> GatewayAskContext:
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    return GatewayAskContext(
        agent,
        paths,
        submitted.request_id,
        submitted.request_path,
        submitted.response_path,
        timeout,
        stream_output=not args.json,
    )


def cmd_gateway_ask(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = wait_for_gateway_running(
        paths,
        timeout=float(getattr(agent.config, "gateway_ready_timeout_seconds", 3) or 3),
    )
    if not alive:
        print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
        return 2

    submitted = _submit_gateway_ask(args, agent, paths)
    if args.no_wait:
        print(f"queued request_id={submitted.request_id}")
        print(f"request: {submitted.request_path}")
        print(f"response: {submitted.response_path}")
        return 0

    # Synchronous mode: poll for streaming chunks and response file.
    ask_ctx = _gateway_ask_context(args, agent, paths, submitted)
    result = _poll_gateway_response(ask_ctx)
    response = result.payload

    if not response:
        return _handle_gateway_timeout(ask_ctx)
    return print_gateway_response(
        response,
        json_mode=args.json,
        show_prompt=args.show_prompt,
        suppress_response=result.streamed_text,
    )


# LLM: CLI result reads the canonical terminal envelope. responses/*.json is only a projection and
# cannot authorize, complete, or replace a request.
# 函数用途: 按请求 ID 显示已封存的 Gateway 最终结果。
def cmd_gateway_result(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    terminal_path = paths.terminal / f"{args.request_id}.json"
    payload = read_gateway_terminal_response_file(
        terminal_path,
        request_id=args.request_id,
        context="gateway.cli.result.terminal.read",
    )
    if not payload:
        print(f"未找到 gateway 终态结果: {args.request_id}", file=sys.stderr)
        print(f"terminal: {terminal_path}")
        return 2
    return print_gateway_response(payload, json_mode=args.json, show_prompt=args.show_prompt)
