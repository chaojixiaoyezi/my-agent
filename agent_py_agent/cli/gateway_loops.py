
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
from datetime import datetime, timezone
from pathlib import Path

from ..agent.concurrency import DurableDaemonThreadPoolExecutor
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
from ..agent.gateway_parts.input_delivery_service import reconcile_gateway_input_receipts
from ..agent.gateway_parts.queue_service import GatewayClaim
from ..agent.gateway_parts.recovery import repair_gateway_terminal_projections
from ..agent.gateway_parts.request_worker import (
    AdmissionLimits,
    _process_claimed_gateway_request_path,
    admission,
    dispatch_pending_requests,
    terminalize_unhandled_claimed_gateway_request,
)
from ..agent.ingestion.continuous_monitor import recover_active_audit_harvesters
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
from .models import GatewayRunContext


# LLM: GatewayRunContext owns the one long-lived base-agent composition root; every base-owner controller and request lane must reuse it, while owner-scoped isolation remains delegated to OwnerScopedAgentPool.
# 函数用途: 取得 Gateway 启动时已经初始化好的基础智能体，禁止后台线程再次加载配置并构造整套智能体。
def _gateway_agent_from_context(context: GatewayRunContext) -> SimpleAgent:
    agent = context.agent
    if agent is None:
        raise RuntimeError("GatewayRunContext 缺少已初始化的 agent")
    return agent


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

    所有执行线程复用 GatewayRunContext 的 owner 级 agent；每轮可变字段由
    ThreadLocalAgentAttribute 隔离，远程用户/群继续由 OwnerScopedAgentPool 分流。
    不再给每个执行线程重复构造 backend、工具表、记忆和 capability cache。
    stale lease 恢复扫描随派发节流跑(原 worker-0 职责收进派发者)。"""

    def __init__(self, context: GatewayRunContext, paths: GatewayPaths) -> None:
        self.context = context
        self.paths = paths
        self.bootstrap_agent = _gateway_agent_from_context(context)
        _attach_shared_owner_registry(self.bootstrap_agent, context)
        self.limits = AdmissionLimits.from_config(self.bootstrap_agent.config)
        # The request file and lease are authoritative.  If a provider/tool
        # call wedges during process shutdown, the next Gateway reclaims that
        # exact request; the old attempt cannot be allowed to hold Python open.
        self._executor = DurableDaemonThreadPoolExecutor(
            max_workers=self.limits.global_inflight, thread_name_prefix="gw-exec"
        )
        self._scan_gate = GatewayInboxScanGate()
        self._recover_throttle = _RecoverThrottle(self.bootstrap_agent)
        self._next_input_reconcile_at = 0.0
        self._next_terminal_projection_at = 0.0

    def tick(self) -> int:
        # New work gets the first bounded slice. Historical projection and
        # reconciliation scans must never sit in front of an interactive request.
        dispatched = 0
        try:
            dispatched = dispatch_pending_requests(
                self.paths,
                self.limits,
                self._submit,
                scan_gate=self._scan_gate,
            )
        except Exception as exc:
            _print_gateway_loop_error("gateway_request_dispatch.iteration", "dispatcher", exc)
        # Recovery and projections are independent durability domains. One
        # damaged receipt must not starve normal jobs or another repair domain.
        if self._recover_throttle.due():
            try:
                recover_gateway_processing_requests(
                    self.paths,
                    startup=False,
                    max_attempts=self.bootstrap_agent.config.gateway_request_max_attempts,
                    timeout_seconds=self.bootstrap_agent.config.gateway_processing_timeout_seconds,
                    agent=self.bootstrap_agent,
                )
            except Exception as exc:
                _print_gateway_loop_error("gateway_request_recovery.iteration", "recovery", exc)
        now = time.monotonic()
        if now >= self._next_terminal_projection_at:
            self._next_terminal_projection_at = now + 0.75
            try:
                repair_gateway_terminal_projections(
                    self.paths,
                    agent=self.bootstrap_agent,
                    limit=16,
                )
            except Exception as exc:
                _print_gateway_loop_error(
                    "gateway_terminal_projection.iteration",
                    "terminal-projector",
                    exc,
                )
        if now >= self._next_input_reconcile_at:
            self._next_input_reconcile_at = now + 0.75
            try:
                reconcile_gateway_input_receipts(
                    self.paths,
                    self.bootstrap_agent,
                    limit=64,
                )
            except Exception as exc:
                _print_gateway_loop_error(
                    "gateway_input_reconcile.iteration",
                    "input-reconciler",
                    exc,
                )
        return dispatched

    def _submit(self, claim: GatewayClaim, user_key: str, conversation_key: str) -> None:
        self._executor.submit(self._execute, claim, user_key, conversation_key)

    def _execute(self, claim: GatewayClaim, user_key: str, conversation_key: str) -> None:
        from ..agent.observability.concurrency_metrics import gateway_worker_busy

        agent = None
        processing_path = claim.path
        gateway_worker_busy(1)
        try:
            agent = self._thread_agent()
            _process_claimed_gateway_request_path(
                agent,
                self.paths,
                processing_path,
                f"gw-exec-{threading.get_ident()}",
                claim=claim,
            )
        except Exception as exc:
            _print_gateway_loop_error("gateway_request_execute", processing_path.stem, exc)
            try:
                terminalize_unhandled_claimed_gateway_request(
                    self.paths,
                    processing_path,
                    exc,
                    agent=agent,
                    expected_execution_attempt_id=claim.execution_attempt_id,
                    expected_lease_epoch=claim.lease_epoch,
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
        return self.bootstrap_agent

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


# 后台会话的全局并发基数：owner 与 thread 都已分车道，超出的保留在持久队列。
# 可由 config background_owner_workers 覆盖，此常量是无配置时的兜底。
_BACKGROUND_OWNER_WORKERS = 8
# 同 owner 后台会话并发上限：一条长会话不再占住整个 owner，但也不允许
# 一个 owner 用大量待处理会话占满全局后台模型池。
_BACKGROUND_THREADS_PER_OWNER = 4
_MEMORY_CURATOR_WORKERS = 2
_BASE_SCHEDULER_KEY = "base"
# 调度器存活心跳节流(秒):>5 分钟没更新即疑死,配合 systemd 快速定位。
_HEARTBEAT_INTERVAL_SECONDS = 60
# 策展每日全局配额(默认 5000 次/天,config curator_daily_quota 覆盖):LLM 提取烧钱,
# 归档类晚一天无害,超限顺延;紧急车道(pending reason)不受此限。
_CURATOR_DAILY_QUOTA = 5000
# 算实际消耗的 run 结果状态:只有真正执行了一次 curator 事务才记账;not_due(interval
# 未到/无新消息)、busy(他人持 lease)、disabled 都没碰 LLM,不算消耗。
_CURATOR_CONSUMED_STATUSES = frozenset({"succeeded", "failed"})


def _agent_memory_enabled(agent: object) -> bool:
    """owner memory_policy 总闸(effective flag):关闭则 curator 不调度。

    拿不到 effective policy 的路径(测试桩/旧装配)视为开启——与「memory_policy.json
    缺失视为开启」的默认语义一致,短路只发生在显式 enabled=false 时。"""
    policy = getattr(agent, "owner_policy", None)
    if policy is None:
        return True
    return bool(getattr(policy, "memory_enabled", True))


def _background_owner_workers(agent: object) -> int:
    value = getattr(getattr(agent, "config", None), "background_owner_workers", _BACKGROUND_OWNER_WORKERS)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _BACKGROUND_OWNER_WORKERS
    return parsed if parsed > 0 else _BACKGROUND_OWNER_WORKERS


# LLM: This configurable cap bounds simultaneous model turns for different
# durable conversation threads owned by one identity; zero/invalid uses the safe default.
# 函数用途: 读取单个用户后台会话的最大并发数。
def _background_threads_per_owner(agent: object) -> int:
    value = getattr(
        getattr(agent, "config", None),
        "background_threads_per_owner",
        _BACKGROUND_THREADS_PER_OWNER,
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _BACKGROUND_THREADS_PER_OWNER
    return parsed if parsed > 0 else _BACKGROUND_THREADS_PER_OWNER


# LLM: Memory extraction has its own bounded lane so low-priority owner backlog
# cannot consume the conversation wake executor; invalid values retain the safe default.
# 函数用途: 读取单 Gateway 同时运行的记忆策展数量，超出的 owner 留待下一轮。
def _memory_curator_workers(agent: object) -> int:
    value = getattr(
        getattr(agent, "config", None),
        "memory_curator_workers",
        _MEMORY_CURATOR_WORKERS,
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _MEMORY_CURATOR_WORKERS
    return parsed if parsed > 0 else _MEMORY_CURATOR_WORKERS


# LLM: This mixin owns only process-local planning/execution slots; durable
# conversation sources and run claims remain in BackgroundMainAgentScheduler/Store.
# 类用途: 为单 Gateway 按 owner 公平提交独立的 thread 后台车道。
class _BackgroundThreadLaneSupervisorMixin:
    # LLM: Planning is single-threaded and model-free; submission is round-robin
    # across owners, bounded globally and per owner, and keyed by durable thread id.
    # Gateway's independent orphan reconciler owns recovery scans, so preparation
    # must skip the duplicate inline sweep before ready lanes are submitted.
    # 函数用途: 公平提交就绪会话，且不让重复孤儿扫描堵住同用户或其它窗口。
    def _submit_ready_thread_ticks(self) -> None:
        global_limit = _background_owner_workers(self._base_agent)
        available = max(0, global_limit - len(self._inflight))
        if available <= 0:
            return
        per_owner_limit = _background_threads_per_owner(self._base_agent)
        schedulers = [
            (_BASE_SCHEDULER_KEY, self._base_scheduler),
            *list(self._owner_schedulers.items()),
        ]
        candidates: list[tuple[object, BackgroundMainAgentScheduler, list[str]]] = []
        planned_at = time.time()
        for owner_key, scheduler in schedulers:
            active_threads = self._active_threads_for_owner(owner_key)
            owner_slots = max(0, per_owner_limit - len(active_threads))
            if owner_slots <= 0:
                continue
            try:
                scheduler.prepare_tick(
                    now=planned_at,
                    include_orphan_supervision=False,
                )
                ready = scheduler.ready_thread_ids(
                    now=planned_at,
                    limit=per_owner_limit + len(active_threads),
                )
            except Exception as exc:
                _print_gateway_loop_error(
                    "gateway_background_main.plan",
                    f"background-main:{owner_key}",
                    exc,
                )
                continue
            pending = [thread_id for thread_id in ready if thread_id not in active_threads]
            if pending:
                candidates.append((owner_key, scheduler, pending[:owner_slots]))
        self._submit_thread_candidates(candidates, available=available)

    # LLM: Candidate submission uses the already-bounded executor and never
    # changes durable source status before the worker acquires its run claim.
    # 函数用途: 按 owner 轮询把候选会话填入剩余全局工位。
    def _submit_thread_candidates(
        self,
        candidates: list[tuple[object, BackgroundMainAgentScheduler, list[str]]],
        *,
        available: int,
    ) -> None:
        executor = self._get_executor()
        while available > 0 and any(rows for _key, _scheduler, rows in candidates):
            for owner_key, scheduler, rows in candidates:
                if available <= 0:
                    break
                if not rows:
                    continue
                thread_id = rows.pop(0)
                lane_key = (owner_key, thread_id)
                self._inflight[lane_key] = executor.submit(
                    self._counted_thread_tick,
                    scheduler,
                    str(owner_key),
                    thread_id,
                )
                available -= 1

    # LLM: In-flight identity is owner + thread; checking only owner would restore
    # the starvation bug by treating independent TUI sessions as one execution.
    # 函数用途: 列出某 owner 正在跑的后台会话 ID。
    def _active_threads_for_owner(self, owner_key: object) -> set[str]:
        return {
            str(key[1])
            for key in self._inflight
            if isinstance(key, tuple) and len(key) == 2 and key[0] == owner_key
        }

    # LLM: Metrics count real conversation model lanes; durable thread claims remain
    # the final duplicate-execution fence across processes.
    # 函数用途: 运行一条会话后台车道并更新并发指标。
    def _counted_thread_tick(
        self,
        scheduler: BackgroundMainAgentScheduler,
        label: str,
        thread_id: str,
    ) -> list[object]:
        background_tick_inflight(1)
        try:
            return self._safe_thread_tick(scheduler, label, thread_id)
        finally:
            background_tick_inflight(-1)

    # LLM: The daemon executor is bounded by the exact configured global limit.
    # 函数用途: 惰性创建全局有界后台会话线程池。
    def _get_executor(self) -> object:
        if self._executor is None:
            self._executor = DurableDaemonThreadPoolExecutor(
                max_workers=_background_owner_workers(self._base_agent),
                thread_name_prefix="bg-owner",
            )
        return self._executor

    # LLM: Shutdown cancels only queued process-local submissions in both lanes;
    # durable conversation and curator sources remain pending for restart recovery.
    # 函数用途: 停止主会话与记忆策展线程池，不删除任何持久任务或记忆游标。
    def shutdown(self) -> None:
        executors = (
            getattr(self, "_executor", None),
            getattr(self, "_curator_executor", None),
        )
        self._executor = None
        self._curator_executor = None
        for executor in executors:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    # LLM: One failed conversation lane is logged and isolated; it cannot terminate
    # the supervisor or consume another thread's durable source.
    # 函数用途: 安全运行指定会话的后台消费。
    def _safe_thread_tick(
        self,
        scheduler: BackgroundMainAgentScheduler,
        label: str,
        thread_id: str,
    ) -> list[object]:
        try:
            return list(scheduler.tick_thread(thread_id) or [])
        except Exception as exc:
            _print_gateway_loop_error(
                "gateway_background_main.iteration",
                f"background-main:{label}:{thread_id}",
                exc,
            )
            return []




class _BackgroundMainSupervisor(_BackgroundThreadLaneSupervisorMixin):
    """后台主代理值守:按 owner + thread 消费各自 store 的唤醒并投真渠道。

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
        # 后台整合按 owner + thread 分车道。持久 run claim 保证同会话单飞，
        # 进程内 in-flight 去重防止同一 thread 重复提交；不同 thread 可在有界池中并发。
        self._executor: object | None = None
        # 记忆策展走独立低优先级车道；不能占满主会话/child lifecycle wake 的工位。
        self._curator_executor: object | None = None
        # 会话运行时 的 active turn 是 thread-scoped；这里也以 durable thread_id 作最小运行车道。
        self._inflight: dict[object, object] = {}
        # 磁盘级唤醒发现(治「睡死叫不醒」§1):登记表是进程内易失结构,网关重启清零、
        # LRU 会逐出,且只有新入站请求才补记;长盯守非阻塞挂起期恰恰没有新请求 →
        # scoped owner 的到点 policy 从此无人消费。这里启动即扫一次、之后按间隔重扫,
        # 把磁盘上「有 enabled policy / 待处理唤醒信号」的 owner 种回登记表,重启自愈。
        self._wake_rescan_interval = _wake_rescan_interval_seconds(self._base_agent)
        self._next_wake_rescan_at = 0.0
        self._wake_discovery_cursor: OwnerWakeCursor | None = None
        # Named Audit harvesters live inside the Gateway process. A clean
        # restart reacquires only sources whose exact persisted parent Audit is
        # still active. Ordinary turn-scoped watches never gain background
        # authority merely because an old state file remains on disk.
        self._watch_recovery_interval = 15.0
        self._next_watch_recovery_at = 0.0
        # 后台记忆策展唤醒:节流扫描 + 独立 in-flight 去重(见 _run_due_curators)。
        self._next_curator_run_at = 0.0
        self._curator_inflight: dict[object, object] = {}
        # 纯记忆 owner 只在拿到 curator worker 后临时实例化；完成即逐出，避免历史用户常驻吃内存。
        self._curator_soft_agent_ids: set[int] = set()
        # 轮转游标让 base、硬 owner 与软 owner 共用有限工位而不饿死后排。
        self._curator_candidate_cursor = 0
        # 策展每日全局配额(见 _run_due_curators):记账落盘到 global_index_dir,重启不丢。
        self._curator_quota_path: Path | None = None
        self._curator_quota_count = 0
        self._curator_quota_date = ""
        # 调度器存活心跳(见 _maybe_write_heartbeat):出事 5 分钟内定位调度循环是否还活着。
        self._heartbeat_path: Path | None = None
        self._next_heartbeat_at = 0.0
        index_dir = getattr(getattr(self._base_agent, "home_paths", None), "global_index_dir", None)
        if index_dir is not None:
            self._curator_quota_path = Path(str(index_dir)) / "curator_daily_quota.json"
            self._heartbeat_path = Path(str(index_dir)) / "background_supervisor_heartbeat.json"
            self._load_curator_quota()

    def _load_curator_quota(self) -> None:
        """启动时恢复当日配额计数(重启不丢);只认当天日期,跨天自然重置。"""
        if self._curator_quota_path is None or not self._curator_quota_path.is_file():
            return
        try:
            payload = json.loads(self._curator_quota_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if not isinstance(payload, dict) or str(payload.get("date") or "") != today:
            return
        try:
            count = max(0, int(payload.get("count") or 0))
        except (TypeError, ValueError):
            return
        self._curator_quota_count = count
        self._curator_quota_date = today

    def _maybe_write_heartbeat(self) -> None:
        """调度器存活心跳:节流写 ts+pid 到 global_index_dir,诊断「调度循环死了没有」。"""
        now = time.time()
        if now < self._next_heartbeat_at:
            return
        self._next_heartbeat_at = now + _HEARTBEAT_INTERVAL_SECONDS
        if self._heartbeat_path is None:
            return
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.write_text(
                json.dumps({"ts": int(now), "pid": os.getpid()}, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            pass

    # LLM: One supervisor pass collects finished thread lanes, performs model-free
    # owner maintenance, then fairly submits ready conversation lanes.
    # 函数用途: 推进单 Gateway 的后台会话调度并录制已完成报告。
    def tick(self) -> bool:
        self._maybe_write_heartbeat()
        self._maybe_seed_wake_pending_owners()
        reports = self._collect_finished_ticks()
        self._sync_owner_schedulers()
        self._recover_active_watch_harvesters()
        self._run_due_curators()
        self._submit_ready_thread_ticks()
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

    def _maybe_seed_wake_pending_owners(self) -> None:
        if self._wake_rescan_interval <= 0:
            return
        now = time.monotonic()
        if now < self._next_wake_rescan_at:
            return
        owners_dir = getattr(getattr(self._base_agent, "home_paths", None), "owners_dir", None)
        if not owners_dir:
            self._next_wake_rescan_at = now + self._wake_rescan_interval
            return
        limit = _positive_int_config(self._base_agent, "owner_agent_pool_max_agents", default=64)
        page = seed_registry_page_from_disk(
            self._registry,
            owners_dir,
            limit=limit,
            after_cursor=self._wake_discovery_cursor,
        )
        self._wake_discovery_cursor = page.next_cursor
        # 一轮发现仍然逐页有界，但只在完整扫完后才进入长间隔。否则第 N 页的活跃
        # owner 在 Gateway 重启后会平白等待 N × rescan_interval，真实长任务看起来
        # 像永久卡住。下一页留给下一个 supervisor tick，避免启动线程内全量阻塞。
        self._next_wake_rescan_at = _next_owner_discovery_at(
            now, self._wake_rescan_interval, page.next_cursor
        )
        if page.seeded:
            print(f"[gateway-background-main] wake-pending owners seeded from disk: {page.seeded}", flush=True)

    # LLM: Conversation schedulers are retained only for registry hard facts;
    # curator-only owners are materialized later inside the bounded memory lane.
    # 函数用途: 同步真正有前台、任务或盯守工作的 owner 会话调度器。
    def _sync_owner_schedulers(self) -> None:
        # 登记本唯一入口:registry snapshot(controller claim_due_owners / wake seed 分页种回)。
        # 曾经的磁盘全量策展扫(186+ owner 全进池)已被登记本替代——全量扫把 64 容量 LRU 池
        # 用假活灌满、真活 owner 被逐出饿死(探针实锤:user-b 的 ordinary_task_resume 永不拉起)。
        snapshot = self._registry.snapshot()
        if not snapshot and not self._owner_schedulers:
            return  # 无活跃 scoped owner(单 owner / 未开 scoping)→ 完全不碰 owner 池,零额外开销
        pool = self._ensure_owner_pool()
        if pool is None:
            return
        # 硬事实 owner(待消费 wake/到期 policy/未完成子代理/盯守路/调度活)必进池。
        # 纯 curator 软 owner 不需要会话 scheduler，也不能在这里批量构建完整 Agent；
        # 它们只在 _run_due_curators 真正拿到有限 worker 时临时进入池。
        for owner in self._registry.hard_snapshot():
            try:
                pool.get(owner, hard=True)
            except Exception as exc:
                _print_gateway_loop_error("gateway_background_main.owner_build", str(getattr(owner, "owner_id", "")), exc)
        active = {id(agent): agent for agent in pool.hard_agents()}
        for key in list(self._owner_schedulers):
            if key not in active and not self._active_threads_for_owner(key):
                self._owner_schedulers.pop(key, None)  # scoped agent 被 LRU 逐出且没在跑 → 丢弃其调度器
        for key, scoped_agent in active.items():
            if key not in self._owner_schedulers:
                self._owner_schedulers[key] = _build_background_scheduler(scoped_agent, self._channels)

    # LLM: Watch recovery inspects only hard owner agents because a persisted
    # active watch is itself a hard registry fact and curator-only owners cannot own one.
    # 函数用途: 为当前确有盯守任务的 owner 恢复采集器，不唤醒纯记忆 owner。
    def _recover_active_watch_harvesters(self, *, now: float | None = None) -> int:
        """Reacquire active named-Audit collectors for active owner lanes.

        The persisted cursor and cross-process lease remain authoritative.
        Repeated calls are idempotent: a live local collector is reused, a live
        foreign lease is left alone, and an expired lease is safely taken over.
        """
        current = time.monotonic() if now is None else float(now)
        next_due = float(getattr(self, "_next_watch_recovery_at", 0.0) or 0.0)
        if current < next_due:
            return 0
        interval = max(
            1.0,
            float(getattr(self, "_watch_recovery_interval", 15.0) or 15.0),
        )
        self._next_watch_recovery_at = current + interval
        agents = [self._base_agent]
        pool = getattr(self, "_owner_pool", None)
        if pool is not None:
            try:
                agents.extend(pool.hard_agents())
            except Exception as exc:
                _print_gateway_loop_error(
                    "gateway_watch_recovery.owner_pool",
                    "watch-recovery",
                    exc,
                )
        ensured = 0
        seen: set[str] = set()
        for agent in agents:
            owner_home = str(
                getattr(getattr(agent, "home_paths", None), "owner_home_dir", "")
                or ""
            ).strip()
            if not owner_home or owner_home in seen:
                continue
            seen.add(owner_home)
            try:
                ensured += recover_active_audit_harvesters(agent)
            except Exception as exc:
                _print_gateway_loop_error(
                    "gateway_watch_recovery.owner",
                    owner_home,
                    exc,
                )
        return ensured

    # LLM: Curator candidates are admitted lazily into a dedicated bounded lane;
    # a rotating cursor and post-run soft eviction bound memory without losing owners.
    # 函数用途: 公平轮转各用户的记忆整理，最多只加载实际可运行的 worker 数量。
    def _run_due_curators(self) -> None:
        """后台记忆策展唤醒:按有限 worker 轮转 base、硬 owner 与纯策展软 owner。

        只负责「按时唤醒」:到期判断/租约/失败退避/journal 回滚全在 run_if_due 内部,
        调度器不做重复判断(curator.py:164 run_if_due 自带 pending/turn/interval/daily 触发)。
        提取批次的 LLM 调用可能耗时几十秒,必须丢进独立有界线程池并做 in-flight 去重。
        它不能复用主会话后台池：历史 owner 积压曾同时占满 8 个工位，让真实 child lifecycle
        wake 排在记忆整理之后。单 owner 异常只记录不中断其他 owner(健壮性)。

        每日全局配额(LLM 提取烧钱,防失控):紧急车道(pending reason/active lease,事故必办)
        不受配额约束;常规车道(interval/daily/turn 轮询,归档类晚一天无害)占用配额,
        当日满了就顺延明天——配额账本落盘,重启不丢。
        """
        now = time.monotonic()
        if now < self._next_curator_run_at:
            return
        self._next_curator_run_at = now + _positive_int_config(
            self._base_agent, "owner_maintenance_scan_interval_seconds", default=60
        )
        soft_agent_ids = getattr(self, "_curator_soft_agent_ids", set())
        self._curator_soft_agent_ids = soft_agent_ids
        pool = getattr(self, "_owner_pool", None)
        _retire_finished_curators(self, soft_agent_ids, pool)
        available = max(
            0,
            _memory_curator_workers(self._base_agent) - len(self._curator_inflight),
        )
        if available <= 0:
            return
        candidates = _curator_candidates(self, pool)
        if not candidates:
            return
        self._curator_candidate_cursor = _submit_due_curators(
            self,
            candidates,
            available,
            soft_agent_ids,
            pool,
        )

    def _curator_daily_quota(self) -> int:
        return _positive_int_config(self._base_agent, "curator_daily_quota", default=_CURATOR_DAILY_QUOTA)

    def _curator_quota_available(self) -> bool:
        """常规车道当日剩余配额检查(不记账)。提交前调用:还有额度才发起 curator run。"""
        quota = self._curator_daily_quota()
        if quota <= 0:
            return True  # 配额关闭 = 不限额
        today = datetime.now(timezone.utc).date().isoformat()
        if self._curator_quota_date != today:
            self._curator_quota_date = today
            self._curator_quota_count = 0
        return self._curator_quota_count < quota

    def _curator_quota_record_consumed(self) -> None:
        """实际执行后记账:只有真正跑了一次 curator 事务(提取/晋升/提交)才扣配额。

        提交前扣的旧语义把「每次 tick 的提交检查」都算消耗——run_if_due 大多返回
        not_due(interval 未到/无新消息),没烧 LLM 也照样扣(真机:当日配额被推到
        3651)。按 run 结果记账后,配额 ≈ 实际 LLM 消耗。按日滚动,落盘重启不丢。"""
        quota = self._curator_daily_quota()
        if quota <= 0:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if self._curator_quota_date != today:
            self._curator_quota_date = today
            self._curator_quota_count = 0
        self._curator_quota_count += 1
        if self._curator_quota_path is not None:
            try:
                self._curator_quota_path.parent.mkdir(parents=True, exist_ok=True)
                self._curator_quota_path.write_text(
                    json.dumps({"date": today, "count": self._curator_quota_count}, sort_keys=True),
                    encoding="utf-8",
                )
            except OSError:
                pass  # 配额账本写失败不阻塞策展(内存计数仍在)

    def _curator_has_pending_reason(self, agent: object) -> bool:
        """紧急车道判定:该 agent 的 curator state 是否有 pending reason/active lease(纯读 state.json)。"""
        owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", "")
        if not owner_home:
            return False
        state_path = Path(str(owner_home)) / "memory" / "curator" / "state.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        return bool(payload.get("pending_reasons") or payload.get("active_lease"))

    def _safe_run_curator(self, agent: object, label: str) -> object:
        try:
            result = agent.memory_curator.run_if_due()
        except Exception as exc:
            _print_gateway_loop_error("gateway_memory_curator.iteration", label, exc)
            return None
        status = str(getattr(result, "status", "") or "").strip().lower()
        if status in _CURATOR_CONSUMED_STATUSES:
            self._curator_quota_record_consumed()
        return result

    # LLM: This executor is a separate low-priority capacity lane; queued
    # conversation wake work must never wait behind memory extraction calls.
    # 函数用途: 惰性创建记忆策展专用线程池，并按配置限制全 Gateway 并发。
    def _get_curator_executor(self) -> object:
        if self._curator_executor is None:
            self._curator_executor = DurableDaemonThreadPoolExecutor(
                max_workers=_memory_curator_workers(self._base_agent),
                thread_name_prefix="memory-curator",
            )
        return self._curator_executor

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


# LLM: Finished curator futures are retired before capacity is computed. Soft
# owner agents are evicted only after their exact future ends; hard/base agents
# remain resident and every eviction failure stays diagnostic-only.
# 函数用途: 清掉已结束的记忆策展 future，并释放这轮临时加载的软 owner agent。
def _retire_finished_curators(
    supervisor: object,
    soft_agent_ids: set[object],
    pool: object | None,
) -> None:
    inflight = getattr(supervisor, "_curator_inflight", {})
    for key, future in list(inflight.items()):
        if not getattr(future, "done", lambda: True)():
            continue
        inflight.pop(key, None)
        if key not in soft_agent_ids:
            continue
        soft_agent_ids.discard(key)
        _evict_soft_curator_agent(pool, key)


# LLM: Soft eviction is memory hygiene, never an authority or scheduler gate.
# A failed eviction must not block another owner's curator lane.
# 函数用途: 尝试释放一个软 owner agent；失败只写诊断，不打断后台循环。
def _evict_soft_curator_agent(pool: object | None, key: object) -> None:
    if pool is None:
        return
    try:
        pool.evict_soft_agent_id(key)
    except Exception as exc:
        _print_gateway_loop_error("gateway_memory_curator.soft_evict", str(key), exc)


# LLM: Candidate order is the fairness input: base agent, resident hard owners,
# then bounded registry snapshots. The rotating cursor owns fairness, not this
# collector, so this function never filters by due state or quota.
# 函数用途: 收集本轮可轮转的基础 agent、硬 owner agent 与软 owner 身份。
def _curator_candidates(
    supervisor: object,
    pool: object | None,
) -> list[tuple[str, object]]:
    candidates: list[tuple[str, object]] = [("agent", supervisor._base_agent)]
    if pool is not None:
        try:
            candidates.extend(("agent", agent) for agent in pool.hard_agents())
        except Exception as exc:
            _print_gateway_loop_error(
                "gateway_memory_curator.owner_pool",
                "memory-curator",
                exc,
            )
    candidates.extend(("owner", owner) for owner in supervisor._registry.soft_snapshot())
    return candidates


# LLM: A soft owner identity is materialized through the one owner pool; base
# and hard candidates are already agents. Construction failure is isolated to
# the exact owner and never converted into a fake no-op curator result.
# 函数用途: 把候选 owner 身份按需加载成 agent，并标记它是否需要稍后软释放。
def _resolve_curator_candidate(
    kind: str,
    candidate: object,
    pool: object | None,
) -> tuple[object | None, bool]:
    if kind != "owner":
        return candidate, False
    if pool is None:
        return None, True
    try:
        return pool.get(candidate, hard=False), True
    except Exception as exc:
        _print_gateway_loop_error(
            "gateway_memory_curator.owner_build",
            str(getattr(candidate, "owner_id", "")),
            exc,
        )
        return None, True


# LLM: Admission reads only typed memory policy, pending reasons, quota, and
# the exact in-flight map. Rejected soft candidates are evicted immediately;
# no model prose or inferred task importance participates.
# 函数用途: 判断一个 agent 本轮是否可提交记忆策展，并释放被策略跳过的软 agent。
def _curator_candidate_is_admitted(
    supervisor: object,
    agent: object,
    *,
    is_soft: bool,
    pool: object | None,
) -> bool:
    key = id(agent)
    if key in supervisor._curator_inflight:
        return False
    memory_enabled = _agent_memory_enabled(agent)
    quota_available = (
        supervisor._curator_has_pending_reason(agent)
        or supervisor._curator_quota_available()
    )
    if memory_enabled and quota_available:
        return True
    if is_soft:
        _evict_soft_curator_agent(pool, key)
    return False


# LLM: Submission is the only helper that mutates the curator in-flight map.
# A soft agent stays resident until its future finishes; submit failure evicts
# it before re-raising so the supervisor retry sees a clean pool.
# 函数用途: 把一个已准入 agent 提交到专用策展线程池，并登记去重 future。
def _submit_curator_candidate(
    supervisor: object,
    agent: object,
    *,
    is_soft: bool,
    soft_agent_ids: set[object],
    pool: object | None,
) -> None:
    key = id(agent)
    try:
        future = supervisor._get_curator_executor().submit(
            supervisor._safe_run_curator,
            agent,
            str(getattr(agent, "owner_id", "local")),
        )
    except Exception:
        if is_soft:
            _evict_soft_curator_agent(pool, key)
        raise
    supervisor._curator_inflight[key] = future
    if is_soft:
        soft_agent_ids.add(key)


# LLM: This loop advances the persisted rotating cursor once per examined
# candidate and consumes capacity only after a future is actually submitted.
# 函数用途: 公平扫描候选并提交不超过 available 的记忆策展工作，返回下一轮游标。
def _submit_due_curators(
    supervisor: object,
    candidates: list[tuple[str, object]],
    available: int,
    soft_agent_ids: set[object],
    pool: object | None,
) -> int:
    index = int(getattr(supervisor, "_curator_candidate_cursor", 0) or 0) % len(candidates)
    examined = 0
    while available > 0 and examined < len(candidates):
        kind, candidate = candidates[index]
        index = (index + 1) % len(candidates)
        examined += 1
        agent, is_soft = _resolve_curator_candidate(kind, candidate, pool)
        if agent is None:
            continue
        if not _curator_candidate_is_admitted(
            supervisor,
            agent,
            is_soft=is_soft,
            pool=pool,
        ):
            continue
        _submit_curator_candidate(
            supervisor,
            agent,
            is_soft=is_soft,
            soft_agent_ids=soft_agent_ids,
            pool=pool,
        )
        available -= 1
    return index


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
        self._base_executor: DurableDaemonThreadPoolExecutor | None = None
        self._owner_executor: DurableDaemonThreadPoolExecutor | None = None
        self._owner_worker_limit = _background_owner_workers(self._base_agent)
        self._base_inflight: object | None = None
        self._owner_inflight: dict[tuple[str, str, str], object] = {}
        self._base_next_at = 0.0
        self._owner_next_at: dict[tuple[str, str, str], float] = {}
        self._next_discovery_at = 0.0

    # LLM: The orphan controller uses a bounded runnable set; never pre-fill the executor's
    # private unbounded queue with every cold owner discovered at startup.
    # 函数用途: 启动 base 与 owner 两条孤儿恢复车道，并在停止时非阻塞回收线程池。
    def run(self, stop_event: threading.Event) -> None:
        # Base/local and every scoped owner are independent controller lanes.
        # Building or scanning one owner may be slow; it must never postpone
        # dead-runner recovery for another owner.  Each lane remains
        # single-flight and all mutations still pass through the same
        # workspace lock, lifecycle gate and durable attempt fencing.
        self._base_executor = DurableDaemonThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="orphan-base",
        )
        self._owner_executor = DurableDaemonThreadPoolExecutor(
            max_workers=self._owner_worker_limit,
            thread_name_prefix="orphan-owner",
        )
        poll_interval = min(1.0, max(0.1, self.interval / 10.0))
        try:
            while not stop_event.is_set():
                try:
                    self._async_tick()
                except Exception as exc:
                    _print_gateway_loop_error(
                        "gateway_orphan_reconcile.tick",
                        "orphan-reconcile",
                        exc,
                    )
                stop_event.wait(poll_interval)
        finally:
            self._shutdown_executors()

    def _async_tick(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else float(now)
        self._collect_finished_sweeps()
        self._submit_base_sweep(current)
        self._submit_owner_sweeps(current)

    def _collect_finished_sweeps(self) -> None:
        future = self._base_inflight
        if future is not None and getattr(future, "done", lambda: True)():
            self._base_inflight = None
            self._consume_sweep_result(future, "base")
        for key in list(self._owner_inflight):
            future = self._owner_inflight[key]
            if not getattr(future, "done", lambda: True)():
                continue
            self._owner_inflight.pop(key, None)
            self._consume_sweep_result(future, "/".join(key))

    @staticmethod
    def _consume_sweep_result(future: object, label: str) -> None:
        try:
            future.result()
        except Exception as exc:
            _print_gateway_loop_error(
                "gateway_orphan_reconcile.owner",
                label,
                exc,
            )

    def _submit_base_sweep(self, now: float) -> None:
        if self._base_inflight is not None or now < self._base_next_at:
            return
        executor = self._base_executor
        if executor is None:
            return
        self._base_next_at = now + self.interval
        self._base_inflight = executor.submit(self._sweep, self._base_agent, "base")

    # LLM: Submit at most the actual worker capacity and pick the newest LRU owners first.
    # Queued cold owners must not hide a just-reconnected long task behind dozens of agent builds.
    # 函数用途: 在有界恢复工位内优先扫描最近活跃的用户，未提交者保留到后续 tick。
    def _submit_owner_sweeps(self, now: float) -> None:
        if now >= self._next_discovery_at:
            self._seed_owner_registry()
            self._next_discovery_at = _next_owner_discovery_at(
                now, self.interval, self._discovery_cursor
            )
        # Orphan supervision is a recovery controller, so it only consumes
        # owners carrying hard runtime facts (unfinished run/task/watch/job or
        # an interactive request). Memory-curator-only owners stay on their
        # dedicated soft lane and must not trigger a full subagent filesystem
        # sweep every interval.
        owners = self._registry.hard_snapshot()
        active_keys = {self._owner_key(owner) for owner in owners}
        for key in list(self._owner_next_at):
            if key not in active_keys and key not in self._owner_inflight:
                self._owner_next_at.pop(key, None)
        executor = self._owner_executor
        if executor is None:
            return
        available_slots = max(0, self._owner_worker_limit - len(self._owner_inflight))
        if available_slots <= 0:
            return
        # ActiveOwnerRegistry 的 LRU 顺序是旧→新；恢复优先新近交互/刚发现的 owner。
        for owner in reversed(owners):
            key = self._owner_key(owner)
            if key in self._owner_inflight:
                continue
            if now < float(self._owner_next_at.get(key, 0.0) or 0.0):
                continue
            self._owner_next_at[key] = now + self.interval
            self._owner_inflight[key] = executor.submit(
                self._sweep_owner,
                owner,
                "/".join(key),
            )
            available_slots -= 1
            if available_slots <= 0:
                break

    def _sweep_owner(
        self,
        owner: OwnerIdentity,
        label: str,
    ) -> dict[str, object]:
        pool = self._ensure_owner_pool()
        if pool is None:
            return {"owner": label, "state": "owner_pool_unavailable"}
        return self._sweep(pool.get(owner), label)

    @staticmethod
    def _owner_key(owner: OwnerIdentity) -> tuple[str, str, str]:
        return (
            str(getattr(owner, "provider", "") or ""),
            str(getattr(owner, "owner_kind", "") or ""),
            str(getattr(owner, "owner_id", "") or ""),
        )

    def _shutdown_executors(self) -> None:
        for name in ("_base_executor", "_owner_executor"):
            executor = getattr(self, name, None)
            setattr(self, name, None)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    def tick(self) -> list[dict[str, object]]:
        self._seed_owner_registry()
        reports = [self._sweep(self._base_agent, "base")]
        pool = self._ensure_owner_pool()
        if pool is None:
            return reports
        # Keep the synchronous/test entry aligned with the long-running loop:
        # soft curator work has its own scheduler and is not an orphan signal.
        for owner in self._registry.hard_snapshot():
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
            self._discovery_cursor = None
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


# LLM: 后台报告日志是事后排障的唯一入口，必须带上真实投递结果、唤醒确认和正文引用；
# 只打印 reason/task/thread 会让“答复到底有没有出去”无法从日志复原。
# 函数用途: 记录每条后台主代理报告的结构化投递事实。
def _record_background_main_reports(agent: SimpleAgent, reports: list[object]) -> None:
    for report in reports:
        response = str(getattr(report, "response", "") or "")
        payload = {
            "status": "background_reported",
            "thread_id": str(getattr(report, "thread_id", "") or ""),
            "task_id": str(getattr(report, "task_id", "") or ""),
            "reason": str(getattr(report, "reason", "") or ""),
            "route_channel": str(getattr(report, "route_channel", "") or ""),
            "route_target": str(getattr(report, "route_target", "") or ""),
            "created_at": float(getattr(report, "created_at", 0.0) or 0.0),
            "delivery_status": str(getattr(report, "delivery_status", "") or ""),
            "delivery_reason": str(getattr(report, "delivery_reason", "") or ""),
            "wake_handled": bool(getattr(report, "wake_handled", False)),
            "commit_kind": str(getattr(report, "commit_kind", "") or ""),
            "message_id": str(getattr(report, "message_id", "") or ""),
            "response_chars": len(response),
            "task_status": str(getattr(report, "task_status", "") or ""),
        }
        log_gateway_event(agent, "gateway_background_main_reported", payload)
        print(
            "[gateway-background-main] "
            f"reason={payload['reason']} task={payload['task_id']} thread={payload['thread_id']} "
            f"delivery={payload['delivery_status']} commit={payload['commit_kind']} "
            f"wake_handled={payload['wake_handled']} chars={payload['response_chars']}",
            flush=True,
        )


def _wake_rescan_interval_seconds(agent: SimpleAgent) -> float:
    value = _float_config(agent, "background_owner_wake_rescan_seconds", default=120.0)
    return max(0.0, value)


# LLM: Pagination remains bounded per controller tick, while an unfinished discovery cycle must
# not inherit the long steady-state rescan delay between adjacent pages.
# 函数用途: 决定 owner 磁盘发现下一次运行时间；有后页就下个 tick 继续，整轮扫完才按配置休眠。
def _next_owner_discovery_at(
    now: float,
    interval: float,
    next_cursor: OwnerWakeCursor | None,
) -> float:
    return float(now) if next_cursor is not None else float(now) + max(0.0, float(interval))


def _positive_int_config(agent: SimpleAgent, key: str, *, default: int) -> int:
    try:
        parsed = int(getattr(agent.config, key))
    except (AttributeError, TypeError, ValueError):
        # 缺字段/非数字(测试桩 SimpleNamespace 等)一律回退默认,不崩——后台循环永不停机
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


# LLM: 心跳是本代"HTTP 已 bind 并可服务"的发布信号，所以它必须带代际锚点 started_at（语义与 state 的
#   started_at 相同），否则同 PID 的上一代残留 running 无法被 status_rendering._record_is_current_generation
#   拒绝，判据只能靠 pid+running 误判就绪。锚点在写入函数内部解析（见 _heartbeat_generation_started_at），
#   调用方签名与发布顺序都不变：心跳循环照旧只传 status/pid，终止清理心跳也不新增参数。
#   本函数只写心跳文件，不改 state。
# 函数用途: 写入一份 Gateway 心跳载荷（含本代 started_at），供外部判活、判就绪与诊断读取。
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
            "started_at": _heartbeat_generation_started_at(paths, pid),
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


# LLM: 心跳的 started_at 必须与 state 的 started_at 同源，所以先读"同 pid 的 state 记录"的 started_at
#   （生命周期所有者发布 running 时写的正是 context.process_started_at），再退到"同 pid 的既有心跳"
#   里已经写下的同一个值——终止清理时 state 已被改写成不含 started_at 的终止记录，只能靠它继承。
#   两条都取不到就写 0.0：判据把 0.0 当"无法证明是旧代"，与补字段之前完全一致，旧格式心跳不会突然被判死。
#   只认同 pid 的记录，绝不把别的进程代的锚点抄过来；不读文件 mtime、不猜时钟、不新增调用方参数。
# 函数用途: 解析本次心跳写入应当携带的代际锚点时间（epoch 秒）。
def _heartbeat_generation_started_at(paths: GatewayPaths, pid: int) -> float:
    for payload in (_read_json_payload(paths.state), _read_json_payload(paths.heartbeat)):
        if _positive_int(payload.get("pid")) != _positive_int(pid):
            continue
        started_at = _positive_float(payload.get("started_at"))
        if started_at > 0:
            return started_at
    return 0.0


# LLM: state/heartbeat 都是非原子写，读旧载荷只为继承一个字段；任何解析/IO 问题都必须降级成空字典，
#   绝不能把异常抛回心跳写入路径（心跳一停，外部会把它当成网关死亡）。
# 函数用途: 尽力读取一份 JSON 对象载荷，失败返回空字典。
def _read_json_payload(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: 时间字段可能是字符串/None/负数（旧记录或人工改过），只接受正数，其余按"没有"处理。
# 函数用途: 把任意值转成正浮点数，非正数或解析失败返回 0.0。
def _positive_float(value: object) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


# LLM: pid 字段同样可能缺失或非法，非正数一律按"不匹配"处理，避免把别的进程代当成本代。
# 函数用途: 把任意值转成正整数，非正数或解析失败返回 0。
def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _print_gateway_loop_error(context: str, worker_id: str, exc: BaseException) -> None:
    report = runtime_error_report(exc, context=context)
    report["worker_id"] = worker_id
    print("[gateway-loop-error] " + json.dumps(report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
