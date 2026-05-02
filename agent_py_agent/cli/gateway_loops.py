from __future__ import annotations

"""LLM: gateway background loop functions — request workers, heartbeat, and worker pool management.

给人看的解释：
这个文件放 gateway 后台跑的各种循环：请求处理 worker 池、心跳写入。
从 gateway_process.py 拆出来，让主入口文件更短。
"""

import os
import sys
import threading
import time

from ..agent.core import SimpleAgent
from ..agent.gateway import (
    GatewayPaths,
    _process_gateway_requests,
    gateway_request_counts,
    recover_gateway_processing_requests,
    write_json_file,
)
from .common import make_agent
from .models import DaemonOptions


def _gateway_request_loop(args, paths: GatewayPaths, stop_event: threading.Event) -> None:
    """LLM: Background thread that manages a pool of request workers for the gateway inbox.

    新手说明:
    启动若干 worker 线程来并发处理 gateway inbox 中的请求。
    每个 worker 有自己的 SimpleAgent 实例，避免共享连接。
    主循环等待 stop_event 触发后退出并 join 所有 worker。
    """

    try:
        bootstrap_agent = make_agent(args)
        worker_count = max(1, int(bootstrap_agent.config.gateway_request_workers or 1))
    except Exception as exc:
        print(f"gateway request worker failed to initialize: {exc}", file=sys.stderr)
        return
    workers: list[threading.Thread] = []
    for index in range(worker_count):
        thread = threading.Thread(
            target=_gateway_request_worker_loop,
            args=(args, paths, stop_event, index),
            daemon=True,
        )
        thread.start()
        workers.append(thread)
    while not stop_event.is_set():
        stop_event.wait(0.5)
    for thread in workers:
        thread.join(timeout=2)


def _gateway_request_worker_loop(args, paths: GatewayPaths, stop_event: threading.Event, worker_index: int) -> None:
    """LLM: Single gateway request worker that polls and processes inbox requests.

    新手说明:
    单个 worker 线程的主循环。worker_index==0 的 worker 还负责
    恢复卡在 processing 状态的旧请求。空闲时按 poll_interval 等待。
    """

    try:
        agent = make_agent(args)
    except Exception as exc:
        print(f"gateway request worker {worker_index} failed to initialize: {exc}", file=sys.stderr)
        return

    poll_interval = max(1, int(agent.config.gateway_request_poll_interval))
    while not stop_event.is_set():
        try:
            if worker_index == 0:
                recover_gateway_processing_requests(
                    paths,
                    startup=False,
                    max_attempts=agent.config.gateway_request_max_attempts,
                    timeout_seconds=agent.config.gateway_processing_timeout_seconds,
                    agent=agent,
                )
            processed = _process_gateway_requests(agent, paths, worker_id=f"gw-worker-{worker_index}")
        except Exception as exc:
            print(f"gateway request worker {worker_index} failed: {exc}", file=sys.stderr)
            processed = 0
        if processed:
            continue
        stop_event.wait(poll_interval)


def _gateway_heartbeat_loop(paths: GatewayPaths, agent: SimpleAgent, options: DaemonOptions, stop_event: threading.Event) -> None:
    """LLM: Periodically writes a heartbeat JSON file so external monitors can detect liveness.

    新手说明:
    按配置的间隔定期写 heartbeat 文件，让外部监控知道 gateway 还活着。
    stop_event 触发后退出循环。
    """

    while not stop_event.is_set():
        _write_gateway_heartbeat(paths, agent, options, status="running", pid=os.getpid())
        stop_event.wait(max(1, agent.config.gateway_heartbeat_interval))


def _write_gateway_heartbeat(
    paths: GatewayPaths,
    agent: SimpleAgent,
    options: DaemonOptions,
    *,
    status: str,
    pid: int,
) -> None:
    """LLM: Write a single heartbeat JSON record to the gateway heartbeat file.

    新手说明:
    把当前 gateway 的状态（pid、workspace、配置、请求计数）写入 heartbeat 文件。
    外部工具可以通过读取这个文件来判断 gateway 是否存活。
    """
    write_json_file(
        paths.heartbeat,
        {
            "status": status,
            "pid": pid,
            "updated_at": time.time(),
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "apply": options.apply,
            "execute_runners": options.execute_runners,
            "planner": options.planner,
            "interval": options.interval,
            "max_runners": options.max_runners,
            "max_cycles": options.max_cycles,
            "request_counts": gateway_request_counts(paths),
        },
    )
