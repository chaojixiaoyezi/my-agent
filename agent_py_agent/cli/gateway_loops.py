
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
from types import SimpleNamespace

from ..agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from ..agent.core import SimpleAgent
from ..agent.delivery import DeliveryService
from ..agent.gateway_parts import (
    GatewayInboxScanGate,
    GatewayPaths,
    gateway_queue_ages,
    gateway_request_counts,
    log_gateway_event,
    recover_gateway_processing_requests,
    write_json_file,
)
from ..agent.gateway_parts.request_worker import (
    AdmissionLimits,
    _process_claimed_gateway_request_path,
    admission,
    dispatch_pending_requests,
    terminalize_unhandled_claimed_gateway_request,
)
from ..agent.observability.concurrency_metrics import background_tick_inflight
from ..agent.owner_scoped_pool import shared_active_owner_registry
from ..agent.owner_wake_discovery import (
    OwnerWakeCursor,
    discover_owner_home_page,
    seed_registry_page_from_disk,
)
from ..agent.runtime_errors import runtime_error_report
from ..agent.scheduler import SchedulerDueIndex
from ..agent.user_space.owner_maintenance import run_owner_retention_if_due
from ..agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from .common import make_agent
from .models import GatewayRunContext


def _gateway_agent_from_context(context: GatewayRunContext) -> SimpleAgent:
    root = getattr(context.agent, "root", "")
    return make_agent(SimpleNamespace(config=str(context.config_path), workspace_root=str(root or "")))


def _gateway_request_loop(context: GatewayRunContext, paths: GatewayPaths, stop_event: threading.Event) -> None:
    """请求处理循环:单派发者扫描 pending,按两层限流(每用户小坑+全局大坑)认领,
    交给按需扩张的执行线程池跑(池上限=全局大坑)。取代原「N 个 worker 线程各自扫描」
    的单层总闸——那个 N 就是旧的全局总 10。"""
    try:
        dispatcher = _RequestDispatcher(context, paths)
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_pool.initialize", "pool", exc)
        return
    poll_interval = _gateway_request_poll_interval(dispatcher.bootstrap_agent)
    while not stop_event.is_set():
        if dispatcher.tick():
            continue
        stop_event.wait(poll_interval)
    dispatcher.shutdown()


class _RequestDispatcher:
    """两层限流派发者:admission(request_worker.admission)→ claim → 提交执行池。

    执行线程首用时懒建【线程私有】agent(与原 per-worker agent 的线程隔离语义一致,
    线程驻留复用,不跨线程共享实例);活跃 owner 登记表仍与后台主代理循环共享。
    stale lease 恢复扫描随派发节流跑(原 worker-0 职责收进派发者)。"""

    def __init__(self, context: GatewayRunContext, paths: GatewayPaths) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.context = context
        self.paths = paths
        self.bootstrap_agent = _gateway_agent_from_context(context)
        self.limits = AdmissionLimits.from_config(self.bootstrap_agent.config)
        self._executor = ThreadPoolExecutor(
            max_workers=self.limits.global_inflight, thread_name_prefix="gw-exec"
        )
        self._scan_gate = GatewayInboxScanGate()
        self._recover_throttle = _RecoverThrottle(self.bootstrap_agent)
        self._thread_agents = threading.local()

    def tick(self) -> int:
        try:
            if self._recover_throttle.due():
                recover_gateway_processing_requests(
                    self.paths,
                    startup=False,
                    max_attempts=self.bootstrap_agent.config.gateway_request_max_attempts,
                    timeout_seconds=self.bootstrap_agent.config.gateway_processing_timeout_seconds,
                    agent=self.bootstrap_agent,
                )
            return dispatch_pending_requests(self.paths, self.limits, self._submit, scan_gate=self._scan_gate)
        except Exception as exc:
            _print_gateway_loop_error("gateway_request_dispatch.iteration", "dispatcher", exc)
            return 0

    def _submit(self, processing_path, user_key: str, conversation_key: str) -> None:
        self._executor.submit(self._execute, processing_path, user_key, conversation_key)

    def _execute(self, processing_path, user_key: str, conversation_key: str) -> None:
        from ..agent.observability.concurrency_metrics import gateway_worker_busy

        gateway_worker_busy(1)
        try:
            agent = self._thread_agent()
            _process_claimed_gateway_request_path(
                agent, self.paths, processing_path, f"gw-exec-{threading.get_ident()}"
            )
        except Exception as exc:
            _print_gateway_loop_error("gateway_request_execute", processing_path.stem, exc)
            try:
                terminalize_unhandled_claimed_gateway_request(
                    self.paths,
                    processing_path,
                    exc,
                )
            except Exception as terminalize_exc:
                _print_gateway_loop_error(
                    "gateway_request_terminalize",
                    processing_path.stem,
                    terminalize_exc,
                )
        finally:
            gateway_worker_busy(-1)
            admission.release(user_key, conversation_key=conversation_key)

    def _thread_agent(self) -> SimpleAgent:
        agent = getattr(self._thread_agents, "agent", None)
        if agent is None:
            agent = _gateway_agent_from_context(self.context)
            _attach_shared_owner_registry(agent, self.context)
            self._thread_agents.agent = agent
        return agent

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


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
    reconcile_thread = _start_orphan_reconcile_loop(context, stop_event)
    maintenance_thread = _start_owner_maintenance_loop(context, stop_event)
    scheduler_due_thread = _start_scheduler_due_loop(context, stop_event)
    try:
        _run_background_main_ticks(supervisor, stop_event, poll_interval)
    finally:
        _shutdown_background_main(
            supervisor,
            (reconcile_thread, maintenance_thread, scheduler_due_thread),
        )


def _run_background_main_ticks(
    supervisor: object,
    stop_event: threading.Event,
    poll_interval: float,
) -> None:
    while not stop_event.is_set():
        if _supervisor_tick_survives(supervisor):
            continue
        stop_event.wait(poll_interval)


def _shutdown_background_main(
    supervisor: object,
    controller_threads: tuple[threading.Thread | None, ...],
) -> None:
    shutdown = getattr(supervisor, "shutdown", None)
    if callable(shutdown):
        shutdown()
    for thread in controller_threads:
        if thread is not None:
            thread.join(timeout=2)


def _start_orphan_reconcile_loop(
    context: GatewayRunContext,
    stop_event: threading.Event,
) -> threading.Thread | None:
    """Start the model-independent orphan controller beside the LLM scheduler.

    A scheduler tick may spend minutes inside one model turn.  Restart recovery
    cannot share that execution slot: a runner heartbeat can still be fresh on
    the first post-restart tick and become reclaimable only while the model turn
    is blocked.  This controller owns no conversation turn; it only reconciles
    structured runner state on its own clock.
    """
    try:
        reconciler = _GatewayOrphanReconciler(context)
    except Exception as exc:
        _print_gateway_loop_error("gateway_orphan_reconcile.initialize", "orphan-reconcile", exc)
        return None
    if reconciler.interval <= 0:
        return None
    thread = threading.Thread(
        target=reconciler.run,
        name="gateway-orphan-reconcile",
        args=(stop_event,),
        daemon=True,
    )
    thread.start()
    return thread


def _start_owner_maintenance_loop(
    context: GatewayRunContext,
    stop_event: threading.Event,
) -> threading.Thread | None:
    try:
        controller = _GatewayOwnerMaintenanceController(context)
    except Exception as exc:
        _print_gateway_loop_error("gateway_owner_maintenance.initialize", "owner-maintenance", exc)
        return None
    if controller.interval <= 0:
        return None
    thread = threading.Thread(
        target=controller.run,
        name="gateway-owner-maintenance",
        args=(stop_event,),
        daemon=True,
    )
    thread.start()
    return thread


def _start_scheduler_due_loop(
    context: GatewayRunContext,
    stop_event: threading.Event,
) -> threading.Thread | None:
    try:
        controller = _GatewaySchedulerDueController(context)
    except Exception as exc:
        _print_gateway_loop_error("gateway_scheduler_due.initialize", "scheduler-due", exc)
        return None
    thread = threading.Thread(
        target=controller.run,
        name="gateway-scheduler-due",
        args=(stop_event,),
        daemon=True,
    )
    thread.start()
    return thread


def _supervisor_tick_survives(supervisor: _BackgroundMainSupervisor) -> bool:
    """永不停机硬保障:tick 的编排缝隙(种子重扫/owner 池同步/提交/汇报)不在 _safe_tick
    保护内,曾能把后台主循环线程整个杀死——网关被 systemd 拉着 active,干活的循环却再也
    不回来(真机 429 断供实锤的死法之一)。任何异常打点后返回 False 等一拍继续,循环只随
    stop_event 退出。"""
    try:
        return bool(supervisor.tick())
    except Exception as exc:
        _print_gateway_loop_error("gateway_background_main.tick", "background-main", exc)
        return False


def _build_background_scheduler(agent: SimpleAgent, channels: DeliveryService) -> BackgroundMainAgentScheduler:
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
#   可由 config background_owner_workers 覆盖(千并发调参入口),此常量是无配置时的兜底。
_BACKGROUND_OWNER_WORKERS = 8
_BASE_SCHEDULER_KEY = "base"


def _background_owner_workers(agent: object) -> int:
    value = getattr(getattr(agent, "config", None), "background_owner_workers", _BACKGROUND_OWNER_WORKERS)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _BACKGROUND_OWNER_WORKERS
    return parsed if parsed > 0 else _BACKGROUND_OWNER_WORKERS


class _BackgroundMainSupervisor:
    """后台主代理值守:tick base owner + 每个活跃 scoped owner,各自消费自己 store 的唤醒并投真渠道。

    修「叫回半环」两处断裂:
    - 多 owner 消费:scoped owner 的子代理把唤醒写进各自 owner 的 conversation_store,base 调度器看不到。
      这里按共享活跃登记表(请求路 record)逐 owner 建**本后台线程私有**的 scoped agent + 调度器 tick;
      scoped agent 只这一条线程用,不跨线程共享实例(on-disk store 本就并发安全)。单 owner / 未开 scoping
      → 登记表空 → 只 tick base,行为不变。
    - 真渠道投递:base 与各 owner 调度器都用 DeliveryService(叫回产出主动外呼飞书),不再 FakeDeliveryService。
    """

    def __init__(self, context: GatewayRunContext) -> None:
        self._base_agent = _gateway_agent_from_context(context)
        self._channels = self._base_agent.delivery_service
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
        # 会话运行时 starts an active goal on its own live thread when that thread is
        # idle.  Keep the same isolation here: base/local and scoped owners are
        # independent lanes.  Running the base scheduler synchronously used to
        # block this supervisor before scoped owner ticks could even be
        # submitted, so one long local goal starved every IM user.
        self._inflight: dict[object, object] = {}
        # 磁盘级唤醒发现(治「睡死叫不醒」§1):登记表是进程内易失结构,网关重启清零、
        # LRU 会逐出,且只有新入站请求才补记;长盯守非阻塞挂起期恰恰没有新请求 →
        # scoped owner 的到点 policy 从此无人消费。这里启动即扫一次、之后按间隔重扫,
        # 把磁盘上「有 enabled policy / 待处理唤醒信号」的 owner 种回登记表,重启自愈。
        self._wake_rescan_interval = _wake_rescan_interval_seconds(self._base_agent)
        self._next_wake_rescan_at = 0.0
        self._wake_discovery_cursor: OwnerWakeCursor | None = None

    def tick(self) -> bool:
        self._maybe_seed_wake_pending_owners()
        reports = self._collect_finished_ticks()
        self._sync_owner_schedulers()
        self._submit_base_tick()
        self._submit_owner_ticks()
        if not reports:
            return False
        _record_background_main_reports(self._base_agent, reports)
        return True

    def _collect_finished_ticks(self) -> list[object]:
        out: list[object] = []
        for key in list(self._inflight):
            future = self._inflight[key]
            if not getattr(future, "done", lambda: True)():
                continue
            self._inflight.pop(key, None)
            out.extend(self._tick_result(future, key))
        return out

    def _tick_result(self, future: object, key: object) -> list[object]:
        try:
            return list(future.result() or [])
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.owner_result", str(key), exc)
            return []

    def _submit_base_tick(self) -> None:
        if _BASE_SCHEDULER_KEY in self._inflight:
            return
        self._inflight[_BASE_SCHEDULER_KEY] = self._get_executor().submit(
            self._safe_tick,
            self._base_scheduler,
            _BASE_SCHEDULER_KEY,
        )

    def _submit_owner_ticks(self) -> None:
        if not self._owner_schedulers:
            return
        executor = self._get_executor()
        for key, scheduler in self._owner_schedulers.items():
            if key in self._inflight:
                continue  # 上一轮该 owner 的 tick 还在跑(或卡死)→ 不重复提交,让其他 owner 照常并行
            self._inflight[key] = executor.submit(self._counted_tick, scheduler, str(key))

    def _counted_tick(self, scheduler: BackgroundMainAgentScheduler, label: str) -> list[object]:
        # §6-A 量化探针:后台整合 tick 在飞 gauge(池上限 _BACKGROUND_OWNER_WORKERS)。
        # 贴上限跑=整合池饱和,新 owner 的唤醒只能等下一轮——多用户唤醒饿死的量化信号。
        background_tick_inflight(1)
        try:
            return self._safe_tick(scheduler, label)
        finally:
            background_tick_inflight(-1)

    def _get_executor(self) -> object:
        if self._executor is None:
            from concurrent.futures import ThreadPoolExecutor

            # One reserved slot keeps the base/local lane from consuming the
            # configured scoped-owner capacity.  Both lane kinds stay
            # single-flight through ``_inflight``.
            self._executor = ThreadPoolExecutor(
                max_workers=_background_owner_workers(self._base_agent) + 1,
                thread_name_prefix="bg-owner",
            )
        return self._executor

    def shutdown(self) -> None:
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _safe_tick(self, scheduler: BackgroundMainAgentScheduler, label: str) -> list[object]:
        try:
            return list(scheduler.tick() or [])
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.iteration", f"background-main:{label}", exc)
            return []

    def _maybe_seed_wake_pending_owners(self) -> None:
        if self._wake_rescan_interval <= 0:
            return
        now = time.monotonic()
        if now < self._next_wake_rescan_at:
            return
        self._next_wake_rescan_at = now + self._wake_rescan_interval
        owners_dir = getattr(getattr(self._base_agent, "home_paths", None), "owners_dir", None)
        if not owners_dir:
            return
        limit = _positive_int_config(self._base_agent, "owner_agent_pool_max_agents", default=64)
        page = seed_registry_page_from_disk(
            self._registry,
            owners_dir,
            limit=limit,
            after_cursor=self._wake_discovery_cursor,
        )
        self._wake_discovery_cursor = page.next_cursor
        if page.seeded:
            print(f"[gateway-background-main] wake-pending owners seeded from disk: {page.seeded}", flush=True)

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


class _GatewayOwnerMaintenanceController:
    """Apply one bounded owner retention page per tick without creating agents."""

    def __init__(self, context: GatewayRunContext) -> None:
        self._base_agent = _gateway_agent_from_context(context)
        self._base_home = self._base_agent.home_paths
        self._cursor: OwnerWakeCursor | None = None
        self.interval = max(
            0.0,
            _float_config(
                self._base_agent,
                "owner_maintenance_scan_interval_seconds",
                default=60.0,
            ),
        )

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                _print_gateway_loop_error(
                    "gateway_owner_maintenance.tick",
                    "owner-maintenance",
                    exc,
                )
            stop_event.wait(self.interval)

    def tick(self, *, now: float | None = None) -> dict[str, int]:
        current = float(now if now is not None else time.time())
        reports = [run_owner_retention_if_due(self._base_home, now=current)]
        limit = _positive_int_config(
            self._base_agent,
            "owner_agent_pool_max_agents",
            default=64,
        )
        page = discover_owner_home_page(
            self._base_home.owners_dir,
            limit=limit,
            after_cursor=self._cursor,
        )
        self._cursor = page.next_cursor
        for target in page.targets:
            owner = resolve_owner_home(self._base_home.root, target.identity)
            scoped_home = home_paths_with_owner(self._base_home, owner)
            try:
                reports.append(run_owner_retention_if_due(scoped_home, now=current))
            except Exception as exc:
                label = "/".join(
                    (
                        target.identity.provider,
                        target.identity.owner_kind,
                        target.identity.owner_id,
                    )
                )
                _print_gateway_loop_error("gateway_owner_maintenance.owner", label, exc)
        summary = {
            "scanned": page.scanned,
            "ran": sum(report.ran for report in reports),
            "failed": sum(
                report.status in {"partial_failure", "policy_unavailable"}
                for report in reports
            ),
        }
        if summary["ran"] or summary["failed"]:
            print(
                "[gateway-owner-maintenance] "
                + json.dumps(summary, ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        return summary


class _GatewaySchedulerDueController:
    """Wake due scoped owners from one indexed projection, then revalidate locally.

    通道运行时 keeps cron jobs in one SQLite store and arms from the earliest
    ``nextRunAt``.  My-agent keeps each owner's JSON ledger as authority, so
    this controller adapts that pattern with a shared SQLite due-owner index.
    It never executes a job or reads another owner's prompt.
    """

    def __init__(self, context: GatewayRunContext) -> None:
        self._base_agent = _gateway_agent_from_context(context)
        self._registry = shared_active_owner_registry(context.agent)
        repository = getattr(self._base_agent, "scheduler_repository", None)
        due_index = getattr(repository, "due_index", None)
        self._due_index = due_index or SchedulerDueIndex(
            self._base_agent.home_paths.global_index_dir / "scheduler_due.sqlite3"
        )
        self.interval = _background_main_poll_interval(self._base_agent)
        pool_limit = _positive_int_config(
            self._base_agent,
            "owner_agent_pool_max_agents",
            default=64,
        )
        self._claim_limit = max(1, min(pool_limit, _background_owner_workers(self._base_agent)))
        self._lease_seconds = max(30.0, self.interval * 6)

    def run(self, stop_event: threading.Event) -> None:
        try:
            repaired = self._due_index.repair_legacy_owner_ledgers(
                self._base_agent.home_paths.owners_dir
            )
            if int(repaired.get("scanned") or 0) or int(repaired.get("errors") or 0):
                print(
                    "[gateway-scheduler-due] legacy repair "
                    + json.dumps(repaired, ensure_ascii=False, sort_keys=True),
                    flush=True,
                )
        except Exception as exc:
            _print_gateway_loop_error("gateway_scheduler_due.repair", "scheduler-due", exc)
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                _print_gateway_loop_error("gateway_scheduler_due.tick", "scheduler-due", exc)
            stop_event.wait(self.interval)

    def tick(self, *, now: float | None = None) -> int:
        rows = self._due_index.claim_due_owners(
            now=now,
            limit=self._claim_limit,
            lease_seconds=self._lease_seconds,
        )
        announced = 0
        for row in rows:
            if row.provider == "local" and row.owner_kind == "main":
                continue
            if row.owner_kind == "group":
                owner = OwnerIdentity.provider_group(row.provider, row.owner_id)
            else:
                owner = OwnerIdentity.provider_user(row.provider, row.owner_id)
            self._registry.record(owner)
            announced += 1
        if announced:
            print(
                f"[gateway-scheduler-due] due owners announced: {announced}",
                flush=True,
            )
        return announced


class _GatewayOrphanReconciler:
    """Reconcile dead runners without waiting for an LLM conversation turn.

    The owner registry is shared with request/background paths, while this
    reconciler has its own scoped-agent pool.  That keeps model execution and
    controller liveness independent; persistence locks remain the authority for
    concurrent state changes.
    """

    def __init__(self, context: GatewayRunContext) -> None:
        self._base_agent = _gateway_agent_from_context(context)
        self._registry = shared_active_owner_registry(context.agent)
        self._owner_pool: object | None = None
        self._discovery_cursor: OwnerWakeCursor | None = None
        self.interval = max(
            0.0,
            _float_config(
                self._base_agent,
                "orphan_supervision_interval_seconds",
                default=60.0,
            ),
        )

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                _print_gateway_loop_error("gateway_orphan_reconcile.tick", "orphan-reconcile", exc)
            stop_event.wait(self.interval)

    def tick(self) -> list[dict[str, object]]:
        self._seed_owner_registry()
        reports = [self._sweep(self._base_agent, "base")]
        pool = self._ensure_owner_pool()
        if pool is None:
            return reports
        for owner in self._registry.snapshot():
            label = "/".join(
                (
                    str(getattr(owner, "provider", "") or ""),
                    str(getattr(owner, "owner_kind", "") or ""),
                    str(getattr(owner, "owner_id", "") or ""),
                )
            )
            try:
                reports.append(self._sweep(pool.get(owner), label))
            except Exception as exc:
                _print_gateway_loop_error("gateway_orphan_reconcile.owner", label, exc)
        return reports

    def _seed_owner_registry(self) -> None:
        owners_dir = getattr(getattr(self._base_agent, "home_paths", None), "owners_dir", None)
        if not owners_dir:
            return
        limit = _positive_int_config(self._base_agent, "owner_agent_pool_max_agents", default=64)
        page = seed_registry_page_from_disk(
            self._registry,
            owners_dir,
            limit=limit,
            after_cursor=self._discovery_cursor,
        )
        self._discovery_cursor = page.next_cursor

    def _ensure_owner_pool(self) -> object | None:
        if self._owner_pool is not None:
            return self._owner_pool
        try:
            from ..agent.gateway_parts.request_worker import _owner_pool

            self._owner_pool = _owner_pool(self._base_agent)
        except Exception as exc:
            _print_gateway_loop_error("gateway_orphan_reconcile.owner_pool", "orphan-reconcile", exc)
        return self._owner_pool

    @staticmethod
    def _sweep(agent: SimpleAgent, label: str) -> dict[str, object]:
        from ..agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
            supervise_stalled_orphans,
        )

        summary = dict(supervise_stalled_orphans(agent) or {})
        report: dict[str, object] = {"owner": label, **summary}
        if any(int(summary.get(key) or 0) for key in ("running_reclaimed", "watch_respawned", "orphans_revived")):
            print(
                "[gateway-orphan-reconcile] " + json.dumps(report, ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        return report


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


def _wake_rescan_interval_seconds(agent: SimpleAgent) -> float:
    value = _float_config(agent, "background_owner_wake_rescan_seconds", default=120.0)
    return max(0.0, value)


def _positive_int_config(agent: SimpleAgent, key: str, *, default: int) -> int:
    try:
        parsed = int(getattr(agent.config, key))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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


def _gateway_heartbeat_loop(context: GatewayRunContext, stop_event: threading.Event) -> None:
    paths = context.paths
    agent = context.agent

    while not stop_event.is_set():
        # 心跳写失败(磁盘满/瞬时 IO 错)不能杀心跳线程:线程一死,外部把"心跳停更"当网关死。
        try:
            _write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
        except Exception as exc:
            _print_gateway_loop_error("gateway_heartbeat.write", "heartbeat", exc)
        stop_event.wait(max(1, agent.config.gateway_heartbeat_interval))


def _write_gateway_heartbeat(
    paths: GatewayPaths,
    agent: SimpleAgent,
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
            "task_continuation": "owner_scoped_event_driven",
            "request_counts": gateway_request_counts(paths, include_archives=False),
            "queue_ages": gateway_queue_ages(paths),
            # 两层限流在飞快照(全局 total + 每用户计数):压测/排障看"谁占着坑"。
            "inflight": admission.snapshot(),
        },
    )


def _print_gateway_loop_error(context: str, worker_id: str, exc: BaseException) -> None:
    report = runtime_error_report(exc, context=context)
    report["worker_id"] = worker_id
    print("[gateway-loop-error] " + json.dumps(report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
