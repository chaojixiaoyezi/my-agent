from __future__ import annotations

"""LLM: implements gateway command dispatch, default startup, ask/result client commands.

给人看的解释：
这个文件是 gateway 的“客户端侧”：确保后台进程启动、投递 ask 请求、读取某个请求结果。
真正的进程生命周期在 gateway_process.py。
"""

import argparse
import json
import sys
import time

from ..agent.gateway import (
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


def cmd_gateway(args) -> int:
    """gateway 命令族入口。"""

    print("请指定 gateway 子命令：start / supervisor-start / status / stop / restart / logs / ask / result / start-all。", file=sys.stderr)
    return 2


def ensure_gateway_started(args) -> int:
    """确保 gateway 后台进程正在运行；未运行时自动启动。

    这是 `my-agent` 无参数默认入口的核心：用户只敲命令名时，不应该先学习
    `gateway start`，程序会自己把后台值班进程拉起来。
    """

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
    """无子命令默认入口：自动启动 gateway，然后进入 gateway chat。"""

    code = ensure_gateway_started(args)
    if code:
        return code

    # 启动后检测未完成任务
    if agent := make_agent(args):
        if agent.config.auto_detect_work_on_startup:
            from ..agent.startup_recovery import (
                detect_active_work,
                format_active_work_summary,
                has_active_work,
            )

            summary = detect_active_work(agent)
            if has_active_work(summary):
                print("\n" + "=" * 60)
                print("进行中任务检测")
                print("=" * 60)
                print(format_active_work_summary(summary))
                print("=" * 60 + "\n")

                # 询问用户是否继续
                if summary.active_task_count > 0:
                    try:
                        response = input("是否继续调度这些任务？[Y/n] ").strip().lower()
                        if response and response not in {"y", "yes", ""}:
                            print("已取消自动调度。")
                            # 不进入 chat，让用户手动决定
                            return 0
                    except (EOFError, KeyboardInterrupt):
                        print("\n已取消。")
                        return 0

    args.gateway = True
    args.gateway_timeout = None
    args.inject = None
    args.prompt_file = None
    args.memory_limit = 5
    args.no_save = False
    return cmd_chat(args)

def cmd_gateway_ask(args) -> int:
    """向正在运行的 gateway 投递一条聊天请求。

    这是未来聊天工具/TUI 的最小原型：
    CLI 只是客户端，把用户消息写进 pending；真正调用模型的是后台 gateway 进程。
    """

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = wait_for_gateway_running(paths, timeout=10.0)
    if not alive:
        print("gateway 未在运行。请先执行: my-agent gateway start", file=sys.stderr)
        return 2

    request_id, request_path, response_path = submit_gateway_ask(
        paths,
        prompt=args.prompt,
        inject=args.inject or [],
        prompt_files=args.prompt_file or [],
        save=not args.no_save,
        include_prompt=bool(args.show_prompt),
        resume_context=resume_context_override(args),
        agent=agent,
    )
    if args.no_wait:
        # 异步模式：只告诉用户“请求已放进队列”，不在当前终端等模型结果。
        print(f"queued request_id={request_id}")
        print(f"request: {request_path}")
        print(f"response: {response_path}")
        return 0

    # 同步模式：轮询 chunk 文件实现流式输出，同时等待 response 文件。
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    chunk_path = gateway_chunk_path(paths, request_id)
    spinner = ThinkingSpinner()
    spinner.start()
    chunks_printed = 0
    deadline = time.time() + max(0.0, timeout)
    response = {}

    while time.time() <= deadline:
        # LLM: read new chunks from the streaming file written by daemon.
        if chunk_path.exists():
            try:
                lines = chunk_path.read_text(encoding="utf-8").splitlines()
                for line in lines[chunks_printed:]:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if chunks_printed == 0:
                        spinner.stop()
                    sys.stdout.write(obj.get("text", ""))
                    sys.stdout.flush()
                    chunks_printed += 1
            except (OSError, json.JSONDecodeError):
                pass

        # LLM: check if final response file has arrived.
        response = read_json_file(response_path)
        if response:
            break
        time.sleep(0.1)

    spinner.stop()

    if not response:
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
    return print_gateway_response(response, json_mode=args.json, show_prompt=args.show_prompt)


def cmd_gateway_result(args) -> int:
    """读取某个 gateway 请求的结果。

    主要服务于 `gateway ask --no-wait`。普通用户以后在聊天工具里不需要手动查，
    聊天适配器会拿这个 response 再发回对应会话。
    """

    agent = make_agent(args)
    paths = gateway_paths(agent)
    payload = read_json_file(gateway_response_path(paths, args.request_id))
    if not payload:
        print(f"未找到 gateway 响应: {args.request_id}", file=sys.stderr)
        print(f"response: {gateway_response_path(paths, args.request_id)}")
        return 2
    return print_gateway_response(payload, json_mode=args.json, show_prompt=args.show_prompt)
