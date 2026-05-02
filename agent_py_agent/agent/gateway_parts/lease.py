from __future__ import annotations

"""LLM: 本模块包含 gateway 请求处理租约（lease）和心跳（heartbeat）相关的函数与常量。

新手说明:
gateway 在处理请求时需要"租约"机制——一个后台线程定期刷新请求文件的心跳时间戳，
告诉其它 worker "这个请求我还在处理"。如果心跳超时，其它 worker 可以接管。
本模块把租约间隔计算、心跳刷新、心跳线程启动和活跃状态查询集中管理。
"""

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent

from .io import read_json_file, write_json_file_atomic
from .logging import _report_gateway_side_effect_error, log_gateway_payload

# Liveness registry: tracks which request IDs have an active heartbeat thread.
_active_heartbeat_request_ids: set[str] = set()


def _gateway_processing_lease_interval(agent: SimpleAgent) -> float:
    """LLM: Return a heartbeat cadence short enough to keep processing leases fresh.

    新手说明:
    这个函数根据 gateway_heartbeat_interval 和 gateway_processing_timeout_seconds 两个配置，
    算出一个合适的心跳间隔。核心逻辑是：心跳间隔不能超过 processing timeout 的 1/3，
    这样即使偶尔丢一次心跳，也不会被误判为超时。最小间隔 0.2 秒，防止过于频繁。

    参数说明:
    agent: SimpleAgent 实例，从中读取 config.gateway_heartbeat_interval 和 config.gateway_processing_timeout_seconds。

    返回说明:
    返回心跳间隔秒数，至少 0.2 秒。
    """
    try:
        gateway_interval = float(agent.config.gateway_heartbeat_interval or 5)
    except (TypeError, ValueError):
        gateway_interval = 5.0
    try:
        processing_timeout = float(agent.config.gateway_processing_timeout_seconds or 900)
    except (TypeError, ValueError):
        processing_timeout = 900.0
    if processing_timeout > 0:
        gateway_interval = min(gateway_interval, max(0.2, processing_timeout / 3.0))
    return max(0.2, gateway_interval)


def _touch_gateway_processing_lease(request_path: Path, *, request_id: str, worker_id: str = "") -> bool:
    """LLM: Refresh the lease heartbeat on a processing request file.

    新手说明:
    这个函数读取请求文件，确认 request_id 匹配后，更新 lease_heartbeat_at 和 updated_at 时间戳，
    然后原子写回文件。如果读取失败、ID 不匹配或写入失败，返回 False。

    参数说明:
    request_path: 处理中的请求文件路径。
    request_id: 请求 ID，用于校验文件内容是否匹配。
    worker_id: 可选的 worker 标识，写入 lease_owner 字段。

    返回说明:
    成功刷新返回 True，否则返回 False。
    """
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


def is_heartbeat_alive_for_request(request_id: str) -> bool:
    """LLM: Check whether a heartbeat thread is currently active for the given request.

    新手说明:
    通过检查全局集合 _active_heartbeat_request_ids 判断某个请求是否还有活跃的心跳线程。
    外部代码用它来避免重复启动心跳，或者判断请求是否还在被处理。

    参数说明:
    request_id: 要检查的请求 ID。

    返回说明:
    有活跃心跳返回 True，否则返回 False。
    """
    return request_id in _active_heartbeat_request_ids


def _start_gateway_processing_lease_heartbeat(
    agent: SimpleAgent,
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> tuple[threading.Event, threading.Thread]:
    """LLM: Start a daemon thread that keeps one processing lease alive during agent.run.

    新手说明:
    这个函数启动一个守护线程，按 _gateway_processing_lease_interval 算出的间隔定期调用
    _touch_gateway_processing_lease 刷新心跳。如果连续 3 次刷新失败，线程会放弃并记录日志。
    返回的 stop_event 可以被调用方 set() 来停止心跳，thread 对象用于 join()。

    参数说明:
    agent: SimpleAgent 实例，用于读取配置和记录日志。
    request_path: 处理中的请求文件路径。
    request_id: 请求 ID。
    worker_id: 可选的 worker 标识。

    返回说明:
    返回 (stop_event, thread) 元组。调用方 set stop_event 来停止心跳，join thread 来等待线程结束。
    """
    stop_event = threading.Event()
    interval = _gateway_processing_lease_interval(agent)
    consecutive_failures = 0
    max_failures = 3
    _active_heartbeat_request_ids.add(request_id)

    def heartbeat_loop() -> None:
        nonlocal consecutive_failures
        try:
            while not stop_event.wait(interval):
                try:
                    if not _touch_gateway_processing_lease(request_path, request_id=request_id, worker_id=worker_id):
                        return
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    _report_gateway_side_effect_error("heartbeat", request_id, exc)
                    if consecutive_failures >= max_failures:
                        log_gateway_payload(
                            agent,
                            {"id": request_id, "status": "heartbeat_abandoned", "failures": consecutive_failures},
                            event_type="gateway_heartbeat_abandoned",
                            request_path=request_path,
                        )
                        return
        finally:
            _active_heartbeat_request_ids.discard(request_id)

    thread = threading.Thread(
        target=heartbeat_loop,
        name=f"gateway-lease-{request_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread
