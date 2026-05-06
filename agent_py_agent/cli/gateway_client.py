from __future__ import annotations

"""LLM: implements gateway command dispatch, default startup, ask/result client commands.

给人看的解释：
这个文件是 gateway 的'客户端侧'：确保后台进程启动、投递 ask 请求、读取某个请求结果。
真正的进程生命周期在 gateway_process.py。
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass

from ..agent.gateway import (
    GatewayAskParams,
    gateway_chunk_path,
    gateway_paths,
    gateway_response_path,
    gateway_running,
    log_gateway_payload,
    print_gateway_response,
    read_json_file,
    submit_gateway_ask,
    wait_for_gateway_response,
    wait_for_gateway_running,
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
    args.memory_limit = 5
    args.no_save = False
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


def _stream_chunk_lines(chunk_path: Path, chunks_printed: int, spinner) -> int:
    if not chunk_path.exists():
        return chunks_printed
    try:
        lines = chunk_path.read_text(encoding="utf-8").splitlines()
        for line in lines[chunks_printed:]:
            chunks_printed += _write_stream_chunk_line(line, chunks_printed, spinner)
    except (OSError, json.JSONDecodeError):
        pass
    return chunks_printed


def _write_stream_chunk_line(line: str, chunks_printed: int, spinner) -> int:
    if not line.strip():
        return 0
    obj = json.loads(line)
    if chunks_printed == 0:
        spinner.stop()
    sys.stdout.write(obj.get("text", ""))
    sys.stdout.flush()
    return 1


def _wait_for_gateway_response(
    chunk_path: Path,
    response_path: Path,
    deadline: float,
    spinner: ThinkingSpinner,
    *,
    stream_output: bool,
) -> dict[str, Any]:
    chunks_printed = 0
    response: dict[str, Any] = {}

    while time.time() <= deadline:
        if stream_output:
            chunks_printed = _stream_chunk_lines(chunk_path, chunks_printed, spinner)
        response = read_json_file(response_path)
        if response:
            break
        time.sleep(0.1)

    return response


def _poll_gateway_response(ctx: GatewayAskContext) -> dict[str, Any]:
    chunk_path = gateway_chunk_path(ctx.paths, ctx.request_id)
    spinner = ThinkingSpinner()
    if ctx.stream_output:
        spinner.start()
    deadline = time.time() + max(0.0, ctx.timeout)
    response = _wait_for_gateway_response(
        chunk_path,
        ctx.response_path,
        deadline,
        spinner,
        stream_output=ctx.stream_output,
    )
    if ctx.stream_output:
        spinner.stop()
    return response


def _handle_gateway_timeout(
    agent,
    request_id: str,
    request_path: Path,
    response_path: Path,
    timeout: float,
) -> int:
    log_gateway_payload(
        agent,
        {
            "id": request_id,
            "kind": "ask",
            "status": "timeout",
            "ok": False,
            "error": f"timeout after {timeout}s",
            "created_at": 0,
            "ended_at": time.time(),
        },
        event_type="gateway_request_timeout",
        request_path=request_path,
        response_path=response_path,
    )
    print(f"gateway 请求等待超时: request_id={request_id} timeout={timeout}s", file=sys.stderr)
    print(f"response: {response_path}")
    return 2


def cmd_gateway_ask(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = wait_for_gateway_running(paths, timeout=10.0)
    if not alive:
        print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
        return 2

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
    if args.no_wait:
        print(f"queued request_id={request_id}")
        print(f"request: {request_path}")
        print(f"response: {response_path}")
        return 0

    # Synchronous mode: poll for streaming chunks and response file.
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    ask_ctx = GatewayAskContext(
        agent,
        paths,
        request_id,
        request_path,
        response_path,
        timeout,
        stream_output=not args.json,
    )
    response = _poll_gateway_response(ask_ctx)

    if not response:
        return _handle_gateway_timeout(agent, request_id, request_path, response_path, timeout)
    return print_gateway_response(response, json_mode=args.json, show_prompt=args.show_prompt)


def cmd_gateway_result(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    payload = read_json_file(gateway_response_path(paths, args.request_id))
    if not payload:
        print(f"未找到 gateway 响应: {args.request_id}", file=sys.stderr)
        print(f"response: {gateway_response_path(paths, args.request_id)}")
        return 2
    return print_gateway_response(payload, json_mode=args.json, show_prompt=args.show_prompt)
