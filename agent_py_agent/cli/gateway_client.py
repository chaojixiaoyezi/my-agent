# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""implements gateway command dispatch, default startup, ask/result client commands.

给人看的解释：
这个文件是 gateway 的'客户端侧'：确保后台进程启动、投递 ask 请求、读取某个请求结果。
真正的进程生命周期在 gateway_process.py。
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from .common import DEFAULT_CAPABILITY_CONFIG, make_agent, resume_context_override
from .gateway_process import cmd_gateway_start
from .thinking_spinner import ThinkingSpinner


# LLM: GatewayAskContext 是gateway CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass
class GatewayAskContext:
    agent: object
    paths: object
    request_id: str
    request_path: object
    response_path: object
    timeout: float
    stream_output: bool = True


# LLM: GatewayPollRequest 是gateway CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class GatewayPollRequest:
    chunk_path: Path
    response_path: Path
    deadline: float
    spinner: ThinkingSpinner
    stream_output: bool


# LLM: cmd_gateway 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_gateway(args) -> int:

    print("请指定 gateway 子命令：start / supervisor-start / status / stop / restart / logs / ask / result / start-all。", file=sys.stderr)
    return 2


# LLM: ensure_gateway_started 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 协调 gateway 请求、进程状态、worker 或本地文件之间的流转。
def ensure_gateway_started(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = gateway_running(paths)
    if alive:
        return 0
    start_args = argparse.Namespace(
        config=args.config,
        capability_config=getattr(args, "capability_config", DEFAULT_CAPABILITY_CONFIG),
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


# LLM: cmd_default 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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
    # LLM: leave chat memory default unresolved so cmd_chat can read AgentConfig.
    args.memory_limit = None
    args.no_save = False
    args.app_scrollback = not bool(getattr(args, "plain", False))
    return cmd_chat(args)


# LLM: _maybe_handle_active_work 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _maybe_handle_active_work(args) -> int | None:
    agent = make_agent(args)
    if not agent or not agent.config.auto_detect_work_on_startup:
        return None
    return _handle_active_work_prompt(agent)


# LLM: _handle_active_work_prompt 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
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


# LLM: _stream_chunk_lines 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _write_stream_chunk_line 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 把报告、摘要或状态写入磁盘，保持输出路径和 JSON 字段稳定。
def _write_stream_chunk_line(line: str, chunks_printed: int, spinner) -> int:
    if not line.strip():
        return 0
    obj = json.loads(line)
    if chunks_printed == 0:
        spinner.stop()
    sys.stdout.write(obj.get("text", ""))
    sys.stdout.flush()
    return 1


# LLM: _flush_stream_chunks 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _flush_stream_chunks(request: GatewayPollRequest, chunks_printed: int) -> int:
    if not request.stream_output:
        return chunks_printed
    return _stream_chunk_lines(request.chunk_path, chunks_printed, request.spinner)


# LLM: _wait_for_gateway_response 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 协调 gateway 请求、进程状态、worker 或本地文件之间的流转。
def _wait_for_gateway_response(request: GatewayPollRequest) -> dict[str, Any]:
    chunks_printed = 0
    response: dict[str, Any] = {}

    while time.time() <= request.deadline:
        chunks_printed = _flush_stream_chunks(request, chunks_printed)
        response = read_json_file(request.response_path)
        if response:
            _flush_stream_chunks(request, chunks_printed)
            break
        time.sleep(0.1)

    return response


# LLM: _poll_gateway_response 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 协调 gateway 请求、进程状态、worker 或本地文件之间的流转。
def _poll_gateway_response(ctx: GatewayAskContext) -> dict[str, Any]:
    chunk_path = gateway_chunk_path(ctx.paths, ctx.request_id)
    spinner = ThinkingSpinner()
    if ctx.stream_output:
        spinner.start()
    deadline = time.time() + max(0.0, ctx.timeout)
    response = _wait_for_gateway_response(GatewayPollRequest(chunk_path, ctx.response_path, deadline, spinner, ctx.stream_output))
    if ctx.stream_output:
        spinner.stop()
    return response


# LLM: _handle_gateway_timeout 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 处理用户输入、快捷命令或事件，并分发到对应动作。
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


# LLM: cmd_gateway_ask 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_gateway_ask(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = wait_for_gateway_running(
        paths,
        timeout=float(getattr(agent.config, "gateway_ready_timeout_seconds", 10) or 10),
    )
    if not alive:
        print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
        return 2
    contract_error = _gateway_contract_file_error(args)
    if contract_error:
        print(contract_error, file=sys.stderr)
        return 2

    request_id, request_path, response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt=args.prompt,
            inject=args.inject or [],
            prompt_files=args.prompt_file or [],
            save=not args.no_save,
            include_prompt=bool(args.show_prompt),
            client_wait=not bool(args.no_wait),
            context_scope=str(getattr(args, "context_scope", "default") or "default"),
            resume_context=resume_context_override(args),
            task_attributes=_gateway_task_attributes_from_args(args),
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
        return _handle_gateway_timeout(ask_ctx)
    return print_gateway_response(response, json_mode=args.json, show_prompt=args.show_prompt)


# LLM: gateway ask accepts explicit task contracts as structured args, never by prompt parsing.
# 函数用途: 将 CLI 参数转换成 run task_attributes，供 delivery_contract 等机器合同使用。
def _gateway_task_attributes_from_args(args) -> dict:
    attrs: dict[str, object] = {}
    contract_file = str(getattr(args, "delivery_contract_file", "") or "").strip()
    if contract_file:
        attrs["delivery_contract_file"] = contract_file
        attrs["max_tool_rounds"] = 32
        attrs["parent_product_write"] = "allow"
        attrs["parent_body_read"] = "allow"
    return attrs


# LLM: gateway contract files must fail before queueing, otherwise background requests fail after doing work.
# 函数用途: 校验显式 delivery_contract_file 是否存在；只看结构化 CLI 参数，不解析 prompt 自然语言。
def _gateway_contract_file_error(args) -> str:
    contract_file = str(getattr(args, "delivery_contract_file", "") or "").strip()
    if not contract_file:
        return ""
    if Path(contract_file).expanduser().is_file():
        return ""
    return f"delivery contract 文件不存在: {contract_file}"


# LLM: cmd_gateway_result 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_gateway_result(args) -> int:

    agent = make_agent(args)
    paths = gateway_paths(agent)
    payload = read_json_file(gateway_response_path(paths, args.request_id))
    if not payload:
        print(f"未找到 gateway 响应: {args.request_id}", file=sys.stderr)
        print(f"response: {gateway_response_path(paths, args.request_id)}")
        return 2
    return print_gateway_response(payload, json_mode=args.json, show_prompt=args.show_prompt)
