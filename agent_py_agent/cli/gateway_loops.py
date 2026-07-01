
from __future__ import annotations

"""gateway background loop functions — request workers, heartbeat, and worker pool management.

给人看的解释：
这个文件放 gateway 后台跑的各种循环：请求处理 worker 池、心跳写入。
从 gateway_process.py 拆出来，让主入口文件更短。
"""

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

from ..agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from ..agent.core import SimpleAgent
from ..agent.gateway_parts import (
    GatewayInboxScanGate,
    GatewayPaths,
    _process_gateway_requests,
    gateway_queue_ages,
    gateway_request_counts,
    log_gateway_event,
    recover_gateway_processing_requests,
    write_json_file,
)
from ..agent.gateway_parts.channel_delivery import GatewayChannelHub
from ..agent.owner_scoped_pool import shared_active_owner_registry
from ..agent.runtime_errors import runtime_error_report
from .common import make_agent
from .models import GatewayRunContext, GatewayRunOptions


def _gateway_agent_from_context(context: GatewayRunContext) -> SimpleAgent:
    root = getattr(context.agent, "root", "")
    return make_agent(SimpleNamespace(config=str(context.config_path), workspace_root=str(root or "")))


def _gateway_request_loop(context: GatewayRunContext, paths: GatewayPaths, stop_event: threading.Event) -> None:

    try:
        bootstrap_agent = _gateway_agent_from_context(context)
        worker_count = max(1, int(bootstrap_agent.config.gateway_request_workers or 1))
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_pool.initialize", "pool", exc)
        return
    workers: list[threading.Thread] = []
    for index in range(worker_count):
        thread = threading.Thread(
            target=_gateway_request_worker_loop,
            args=(context, paths, stop_event, index),
            daemon=True,
        )
        thread.start()
        workers.append(thread)
    while not stop_event.is_set():
        stop_event.wait(0.5)
    for thread in workers:
        thread.join(timeout=max(0, int(getattr(bootstrap_agent.config, "gateway_worker_join_timeout_seconds", 2) or 0)))


def _gateway_request_worker_loop(
    context: GatewayRunContext,
    paths: GatewayPaths,
    stop_event: threading.Event,
    worker_index: int,
) -> None:

    try:
        agent = _gateway_agent_from_context(context)
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_worker.initialize", str(worker_index), exc)
        return

    # worker 建的是自己的 agent(线程隔离),但活跃 owner 登记表要与后台主代理循环共享(挂在 context.agent
    # 上):worker 解析出 scoped owner 时往里 record,后台循环据此逐 owner 叫回。best-effort,失败不影响处理。
    _attach_shared_owner_registry(agent, context)
    poll_interval = _gateway_request_poll_interval(agent)
    worker = _WorkerLoopContext(
        agent=agent,
        paths=paths,
        worker_index=worker_index,
        scan_gate=GatewayInboxScanGate(),
        recover_throttle=_RecoverThrottle(agent),
    )
    while not stop_event.is_set():
        if _worker_iteration(worker):
            continue
        stop_event.wait(poll_interval)


@dataclass(frozen=True)
class _WorkerLoopContext:
    """单个 request worker 的循环上下文：扫描门与恢复节流随 worker 存活。"""

    agent: SimpleAgent
    paths: GatewayPaths
    worker_index: int
    scan_gate: GatewayInboxScanGate
    recover_throttle: _RecoverThrottle


def _worker_iteration(worker: _WorkerLoopContext) -> int:
    try:
        if worker.worker_index == 0 and worker.recover_throttle.due():
            _recover_gateway_requests_if_primary(worker.agent, worker.paths, worker.worker_index)
        return _process_gateway_requests(
            worker.agent,
            worker.paths,
            worker_id=f"gw-worker-{worker.worker_index}",
            scan_gate=worker.scan_gate,
        )
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_worker.iteration", str(worker.worker_index), exc)
        return 0


def _attach_shared_owner_registry(agent: SimpleAgent, context: GatewayRunContext) -> None:
    """把共享活跃 owner 登记表(挂在 context.agent 上)同步给本 worker 的隔离 agent。best-effort。"""
    try:
        agent._active_owner_registry = shared_active_owner_registry(context.agent)
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_worker.owner_registry", "owner-registry", exc)


def _gateway_background_main_loop(context: GatewayRunContext, stop_event: threading.Event) -> None:
    try:
        supervisor = _BackgroundMainSupervisor(context)
        poll_interval = supervisor.poll_interval
    except Exception as exc:
        _print_gateway_loop_error("gateway_background_main.initialize", "background-main", exc)
        return

    while not stop_event.is_set():
        if supervisor.tick():
            continue
        stop_event.wait(poll_interval)


def _build_background_scheduler(agent: SimpleAgent, channels: GatewayChannelHub) -> BackgroundMainAgentScheduler:
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=channels)
    return BackgroundMainAgentScheduler(
        {
            "runtime": runtime,
            "store": agent.conversation_store,
            "collaboration_store": getattr(agent, "collaboration_store", None),
        }
    )


# 后台 owner 整合并行度上限:每个活跃 owner 的整合 tick 在独立线程跑,防一个长/卡死 turn 饿死其他
#   owner。够覆盖同时活跃的大任务用户数;超出的排队(下一轮 tick 再提交),不至于线程爆炸。
_BACKGROUND_OWNER_WORKERS = 8


class _BackgroundMainSupervisor:
    """后台主代理值守:tick base owner + 每个活跃 scoped owner,各自消费自己 store 的唤醒并投真渠道。

    修「叫回半环」两处断裂:
    - 多 owner 消费:scoped owner 的子代理把唤醒写进各自 owner 的 conversation_store,base 调度器看不到。
      这里按共享活跃登记表(请求路 record)逐 owner 建**本后台线程私有**的 scoped agent + 调度器 tick;
      scoped agent 只这一条线程用,不跨线程共享实例(on-disk store 本就并发安全)。单 owner / 未开 scoping
      → 登记表空 → 只 tick base,行为不变。
    - 真渠道投递:base 与各 owner 调度器都用 GatewayChannelHub(叫回产出主动外呼飞书),不再 FakeChannelHub。
    """

    def __init__(self, context: GatewayRunContext) -> None:
        self._base_agent = _gateway_agent_from_context(context)
        self._channels = GatewayChannelHub(self._base_agent.config)
        self._base_scheduler = _build_background_scheduler(self._base_agent, self._channels)
        self.poll_interval = _background_main_poll_interval(self._base_agent)
        self._registry = shared_active_owner_registry(context.agent)
        self._owner_pool: object | None = None
        self._owner_schedulers: dict[int, BackgroundMainAgentScheduler] = {}
        # 多 owner 整合并行化:每个 owner 的 tick(可能跑一个数分钟的整合 turn,甚至因 run_command
        #   挂起而卡死)丢进线程池独立跑,不再串行阻塞——否则一个 owner 的长/卡死 turn 会饿死其他
        #   owner 的整合(真机实锤:3 并发用户,先派的把单后台线程占死,后两个整合永不触发→产出残缺)。
        #   in-flight 去重:同 owner 上一轮 tick 没跑完就不重复提交(防同 owner 并发 + 防卡死 turn 被反复起)。
        self._executor: object | None = None
        self._inflight: dict[int, object] = {}

    def tick(self) -> bool:
        reports: list[object] = []
        reports.extend(self._safe_tick(self._base_scheduler, "base"))
        self._sync_owner_schedulers()
        reports.extend(self._collect_finished_owner_ticks())
        self._submit_owner_ticks()
        if not reports:
            return False
        _record_background_main_reports(self._base_agent, reports)
        return True

    def _collect_finished_owner_ticks(self) -> list[object]:
        out: list[object] = []
        for key in list(self._inflight):
            future = self._inflight[key]
            if not getattr(future, "done", lambda: True)():
                continue
            self._inflight.pop(key, None)
            out.extend(self._owner_tick_result(future, key))
        return out

    def _owner_tick_result(self, future: object, key: int) -> list[object]:
        try:
            return list(future.result() or [])
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.owner_result", str(key), exc)
            return []

    def _submit_owner_ticks(self) -> None:
        if not self._owner_schedulers:
            return
        executor = self._get_executor()
        for key, scheduler in self._owner_schedulers.items():
            if key in self._inflight:
                continue  # 上一轮该 owner 的 tick 还在跑(或卡死)→ 不重复提交,让其他 owner 照常并行
            self._inflight[key] = executor.submit(self._safe_tick, scheduler, str(key))

    def _get_executor(self) -> object:
        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor

            self._executor = ThreadPoolExecutor(
                max_workers=_BACKGROUND_OWNER_WORKERS, thread_name_prefix="bg-owner"
            )
        return self._executor

    def _safe_tick(self, scheduler: BackgroundMainAgentScheduler, label: str) -> list[object]:
        try:
            return list(scheduler.tick() or [])
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.iteration", f"background-main:{label}", exc)
            return []

    def _sync_owner_schedulers(self) -> None:
        snapshot = self._registry.snapshot()
        if not snapshot and not self._owner_schedulers:
            return  # 无活跃 scoped owner(单 owner / 未开 scoping)→ 完全不碰 owner 池,零额外开销
        pool = self._ensure_owner_pool()
        if pool is None:
            return
        for owner in snapshot:
            try:
                pool.get(owner)  # get-or-build 该 owner 的作用域 agent(池内缓存 + LRU)
            except Exception as exc:
                _print_gateway_loop_error("gateway_background_main.owner_build", str(getattr(owner, "owner_id", "")), exc)
        active = {id(agent): agent for agent in pool.active_agents()}
        for key in list(self._owner_schedulers):
            if key not in active and key not in self._inflight:
                self._owner_schedulers.pop(key, None)  # scoped agent 被 LRU 逐出且没在跑 → 丢弃其调度器
        for key, scoped_agent in active.items():
            if key not in self._owner_schedulers:
                self._owner_schedulers[key] = _build_background_scheduler(scoped_agent, self._channels)

    def _ensure_owner_pool(self) -> object | None:
        if self._owner_pool is not None:
            return self._owner_pool
        try:
            from ..agent.gateway_parts.request_worker import _owner_pool

            self._owner_pool = _owner_pool(self._base_agent)  # 后台线程私有池,复用请求路同款构建(config 去固化路径)
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.owner_pool", "background-main", exc)
            self._owner_pool = None
        return self._owner_pool


def _record_background_main_reports(agent: SimpleAgent, reports: list[object]) -> None:
    for report in reports:
        payload = {
            "status": "background_reported",
            "thread_id": str(getattr(report, "thread_id", "") or ""),
            "task_id": str(getattr(report, "task_id", "") or ""),
            "reason": str(getattr(report, "reason", "") or ""),
            "route_channel": str(getattr(report, "route_channel", "") or ""),
            "route_target": str(getattr(report, "route_target", "") or ""),
            "created_at": float(getattr(report, "created_at", 0.0) or 0.0),
        }
        log_gateway_event(agent, "gateway_background_main_reported", payload)
        print(
            "[gateway-background-main] "
            f"reason={payload['reason']} task={payload['task_id']} thread={payload['thread_id']}",
            flush=True,
        )


def _background_main_poll_interval(agent: SimpleAgent) -> float:
    request_interval = _float_config(agent, "gateway_request_poll_interval", default=1.0)
    heartbeat_interval = _float_config(agent, "gateway_heartbeat_interval", default=5.0)
    return max(1.0, min(5.0, request_interval, heartbeat_interval))


def _gateway_request_poll_interval(agent: SimpleAgent) -> float:
    return max(0.05, _float_config(agent, "gateway_request_poll_interval", default=0.2))


def _float_config(agent: SimpleAgent, key: str, *, default: float) -> float:
    try:
        return float(getattr(agent.config, key))
    except (TypeError, ValueError):
        return default


class _RecoverThrottle:
    """worker-0 的 stale lease 恢复扫描节流：按 processing 超时的 1/3（至少 2 秒）执行。

    恢复扫描要遍历 processing 目录并逐文件读 JSON，原来每个轮询周期（0.2s）都跑，
    空闲时是主要的无效 IO；节流后检测延迟上界仍远小于 lease 超时。"""

    def __init__(self, agent: SimpleAgent) -> None:
        timeout = _float_config(agent, "gateway_processing_timeout_seconds", default=120.0)
        self._interval = max(2.0, timeout / 3.0 if timeout > 0 else 30.0)
        self._next_at = 0.0

    def due(self) -> bool:
        now = time.monotonic()
        if now < self._next_at:
            return False
        self._next_at = now + self._interval
        return True


def _recover_gateway_requests_if_primary(agent: SimpleAgent, paths: GatewayPaths, worker_index: int) -> None:
    if worker_index != 0:
        return
    recover_gateway_processing_requests(
        paths,
        startup=False,
        max_attempts=agent.config.gateway_request_max_attempts,
        timeout_seconds=agent.config.gateway_processing_timeout_seconds,
        agent=agent,
    )


def _gateway_heartbeat_loop(context: GatewayRunContext, stop_event: threading.Event) -> None:
    paths = context.paths
    agent = context.agent
    options = context.options

    while not stop_event.is_set():
        _write_gateway_heartbeat(paths, agent, options, status="running", pid=os.getpid())
        stop_event.wait(max(1, agent.config.gateway_heartbeat_interval))


def _write_gateway_heartbeat(
    paths: GatewayPaths,
    agent: SimpleAgent,
    options: GatewayRunOptions,
    *,
    status: str,
    pid: int,
) -> None:
    write_json_file(
        paths.heartbeat,
        {
            "status": status,
            "pid": pid,
            "updated_at": time.time(),
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "mutate_state": options.mutate_state,
            "start_runners": options.start_runners,
            "planner": options.planner,
            "interval": options.interval,
            "max_runners": options.max_runners,
            "max_cycles": options.max_cycles,
            "request_counts": gateway_request_counts(paths, include_archives=False),
            "queue_ages": gateway_queue_ages(paths),
        },
    )


def _print_gateway_loop_error(context: str, worker_id: str, exc: BaseException) -> None:
    report = runtime_error_report(exc, context=context)
    report["worker_id"] = worker_id
    print("[gateway-loop-error] " + json.dumps(report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
