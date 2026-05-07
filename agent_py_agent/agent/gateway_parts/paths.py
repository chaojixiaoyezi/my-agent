# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""resolves typed gateway and adapter filesystem path contracts from config.

这个文件只负责"gateway 和 adapter 的文件都放在哪"。
以后命令行、后台进程、测试都从这里拿路径，不需要到处手写目录名。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: GatewayPaths 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关路径字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发请求队列、租约文件、进程状态和响应渲染相关副作用，需保持公开契约稳定。
@dataclass
class GatewayPaths:

    root: Path
    pid: Path
    adapter_pid: Path
    state: Path
    heartbeat: Path
    stop_request: Path
    log: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    responses: Path
    history: Path


# LLM: AdapterPaths 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存adapter路径字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发请求队列、租约文件、进程状态和响应渲染相关副作用，需保持公开契约稳定。
@dataclass
class AdapterPaths:

    root: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    outbox: Path


# LLM: gateway_paths 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def gateway_paths(agent: SimpleAgent) -> GatewayPaths:

    root = agent.root / agent.config.gateway_workspace
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )


# LLM: gateway_chunk_path 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关chunk路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def gateway_chunk_path(paths: GatewayPaths, request_id: str) -> Path:
    return paths.processing / f"{request_id}.chunks.jsonl"


# LLM: adapter_paths 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理adapter路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def adapter_paths(agent: SimpleAgent) -> AdapterPaths:

    root = agent.root / agent.config.adapter_workspace
    return AdapterPaths(
        root=root,
        inbox=root / "inbox",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        outbox=root / "outbox",
    )