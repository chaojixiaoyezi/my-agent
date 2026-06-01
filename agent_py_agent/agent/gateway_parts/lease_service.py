# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""Gateway lease management for request processing.

This module provides lease lifecycle management for gateway requests
including heartbeat threads and lease state updates.
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_float
from .audit_service import audit_heartbeat_abandoned
from .io import read_json_file, write_json_file_atomic
from .logging import _report_gateway_side_effect_error

if TYPE_CHECKING:
    from ...core import SimpleAgent


# Tracks active heartbeat threads per request ID
_active_heartbeat_request_ids: set[str] = set()


# LLM: _HeartbeatLoopContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存heartbeat循环上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _HeartbeatLoopContext:

    agent: SimpleAgent
    request_path: Path
    request_id: str
    worker_id: str
    stop_event: threading.Event
    interval: float


# LLM: get_active_heartbeat_request_ids 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询activeheartbeat请求ids需要的状态，返回调用方可继续处理的快照；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def get_active_heartbeat_request_ids() -> set[str]:
    return _active_heartbeat_request_ids.copy()


# LLM: is_heartbeat_alive_for_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 判断heartbeatalive请求条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def is_heartbeat_alive_for_request(request_id: str) -> bool:
    return request_id in _active_heartbeat_request_ids


# LLM: refresh_processing_lease 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理refreshprocessing租约相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def refresh_processing_lease(
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> bool:
    payload = read_json_file(request_path)
    if not payload:
        return False
    payload_id = str(payload.get("id") or request_path.stem)
    if payload_id != request_id:
        return False
    now = time.time()
    payload["id"] = payload_id
    payload["status"] = "processing"
    if worker_id:
        payload["lease_owner"] = worker_id
    else:
        payload.setdefault("lease_owner", "")
    payload.setdefault("lease_started_at", now)
    payload["lease_heartbeat_at"] = now
    payload["updated_at"] = now
    try:
        write_json_file_atomic(request_path, payload)
    except OSError as exc:
        _report_gateway_side_effect_error("gateway_lease_heartbeat", request_id, exc)
        return False
    return True


# LLM: _lease_interval 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理租约interval相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _lease_interval(agent: SimpleAgent) -> float:
    gateway_interval = _config_float(agent, "gateway_heartbeat_interval")
    processing_timeout = _config_float(agent, "gateway_processing_timeout_seconds")
    if processing_timeout > 0:
        gateway_interval = min(gateway_interval, max(0.2, processing_timeout / 3.0))
    return max(0.2, gateway_interval)


# LLM: _config_float keeps gateway lease-service timing behind the shared settings/defaults boundary.
# 函数用途: 读取 gateway heartbeat/processing timeout 配置，非法值只回退到配置 schema 默认入口，避免 lease service 内另藏默认数字。
def _config_float(agent: SimpleAgent, key: str) -> float:
    try:
        value = float(getattr(agent.config, key))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return default_config_float(key)


# LLM: start_lease_heartbeat 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进租约heartbeat的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def start_lease_heartbeat(
    agent: SimpleAgent,
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    interval = _lease_interval(agent)
    _active_heartbeat_request_ids.add(request_id)
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(_HeartbeatLoopContext(agent, request_path, request_id, worker_id, stop_event, interval),),
        name=f"gateway-lease-{request_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


# LLM: _heartbeat_loop 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理heartbeat循环相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _heartbeat_loop(context: _HeartbeatLoopContext) -> None:
    try:
        _run_heartbeat_loop_body(context)
    finally:
        _active_heartbeat_request_ids.discard(context.request_id)


# LLM: _run_heartbeat_loop_body 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进heartbeat循环body的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _run_heartbeat_loop_body(context: _HeartbeatLoopContext) -> None:
    consecutive_failures = 0
    while not context.stop_event.wait(context.interval):
        ok, consecutive_failures = _refresh_or_count_failure(
            context.request_path, context.request_id, context.worker_id, consecutive_failures
        )
        if ok or not _should_stop_heartbeat(
            context.agent,
            context.request_path,
            context.request_id,
            consecutive_failures,
        ):
            continue
        return


# LLM: _should_stop_heartbeat 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 判断heartbeat条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _should_stop_heartbeat(
    agent: SimpleAgent,
    request_path: Path,
    request_id: str,
    consecutive_failures: int,
) -> bool:
    if consecutive_failures <= 0:
        return True
    if consecutive_failures < 3:
        return False
    audit_heartbeat_abandoned(agent, request_id, consecutive_failures, request_path)
    return True


# LLM: _refresh_or_count_failure 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理refresh数量失败相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _refresh_or_count_failure(
    request_path: Path,
    request_id: str,
    worker_id: str,
    consecutive_failures: int,
) -> tuple[bool, int]:
    try:
        if not refresh_processing_lease(request_path, request_id=request_id, worker_id=worker_id):
            return False, 0
        return True, 0
    except Exception as exc:
        _report_gateway_side_effect_error("heartbeat", request_id, exc)
        return False, consecutive_failures + 1
