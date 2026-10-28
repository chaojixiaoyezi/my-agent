# LLM: bg-owner宿主停止复用准确claim中断与原job观察，共用原两秒预算；未退/未结交cleanup保持incomplete。
# LLM: 原审批清理债务的真实异常链保同outer交原Future；普通无债错误保持原终态路径，查链不替代canonical核收。

# LLM: 单Gateway的请求、后台和维护车道编排；Agent仅用于类型注解，不得因管理模块传递导入而加载core，执行权仍归原队列/会话层。
#   C派发复用原持久gate/claim和同代execution，原线程池/Count0公平及恢复分页保持；线程返回不释放P3。
#   C孤儿业务只走原独立来源请求，这里的旧owner扫车道不直接物化远端Agent；标志只收窄不授执行。
#   C冷组合只运行同源metadata producer与请求盖章，心跳末写核原lease；Count0业务公平、三旧控制线程顺序不变。
#   C输入段仅接原queued/cursor元数据源，绑定输入延期不构造foreign Agent；Count0旧receipt/guidance链保持。
#   单owner规划阶段跨拍复用SQLite物理连接；离开规划即解除Context，不跨提交和后台模型车道。
# 模块用途: 连接请求处理、公平调度、维护和心跳；冷owner预检委托记忆公开读口，不解析内部state。

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.concurrency import DurableDaemonThreadPoolExecutor
from ..agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from ..agent.conversation.background_shutdown import BackgroundShutdown
from ..agent.delivery import DeliveryService
from ..agent.gateway_parts import (
    GatewayInboxScanGate,
    GatewayPaths,
    gateway_queue_ages,
    gateway_request_counts,
    hang_diagnostics,
    log_gateway_event,
    recover_gateway_processing_requests,
    write_json_file,
)
from ..agent.gateway_parts.input_delivery_service import (
    raise_if_input_reconcile_unsettled,
    reconcile_gateway_input_receipts,
)
from ..agent.gateway_parts.io import cleanup_orphan_lock_files, gateway_lock_sidecar_dirs
from ..agent.gateway_parts.lease_service import GATEWAY_HEARTBEAT_INTERVAL_SECONDS
from ..agent.gateway_parts.loop_health import (
    DISPATCHER_STALL_TIMEOUT_SECONDS,
    evaluate_dispatcher_stall,
    loop_health,
)
from ..agent.gateway_parts.queue_service import GatewayClaim
from ..agent.gateway_parts.recovery import repair_gateway_terminal_projections
from ..agent.gateway_parts.request_worker import (
    AdmissionLimits,
    _process_claimed_gateway_request_path,
    admission,
    dispatch_pending_requests,
    terminalize_unhandled_claimed_gateway_request,
)
from ..agent.gateway_parts.restart_service import RESTART_DRAIN_HOLD_REASON, restart_draining
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
from ..agent.settings.thread_model_selection import (
    thread_model_fingerprint,
    thread_model_is_configured,
)
from ..agent.user_space.owner_maintenance import OwnerMaintenanceResult, run_owner_retention_if_due
from ..agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from .gateway_lane_retry import BackgroundLaneRetry
from .gateway_loop_backoff import LoopErrorBackoff
from .models import GatewayRunContext

if TYPE_CHECKING:
    from ..agent.core import SimpleAgent

# 参数减量第 3 批 B 组（2026-09-27）：后台车道的三个节奏不再是配置项（原 gateway_request_poll_interval /
# background_main_error_backoff_seconds / background_owner_wake_rescan_seconds），值不变；心跳节奏见 lease_service。
GATEWAY_REQUEST_POLL_INTERVAL_SECONDS = 0.2  # request worker 空闲时多久检查一次 pending
# 后台主代理车道出错后冷却 30 秒再重试，避免崩溃循环空转
BACKGROUND_MAIN_ERROR_BACKOFF_SECONDS = 30.0  # 后台普通宿主异常按 owner/thread 冷却多久再重试
# owner 唤醒发现整轮磁盘扫描结束后休眠 120 秒再扫下一轮
BACKGROUND_OWNER_WAKE_RESCAN_SECONDS = 120.0  # 磁盘级 owner 唤醒发现整轮扫完后休眠多久
# 启动后暂缓非紧急后台维护的交互保护窗口秒数，不延迟撤销或恢复。
STARTUP_GRACE_WINDOW_SECONDS = 180.0
# 每拍后台维护最多冷构造的策展器数量，热实例不占此额度。
_COLD_CURATOR_BUILDS_PER_TICK_COUNT = 2


# LLM: 只节流非紧急维护，不用于取消、fencing、权限撤销、租约或未知结果恢复。
# 函数用途: 检查本 controller 是否仍在启动交互保护窗口。
def _startup_grace_active(controller) -> bool:
    started = getattr(controller, "_startup_started_at", None)
    return started is not None and time.monotonic() - started < STARTUP_GRACE_WINDOW_SECONDS


# LLM: 不构造冷owner，复用原repo死亡证明/CAS；每repo失败只报warning，不阻断其它owner或retention。
# 函数用途: 维护首拍及周期清理基础和已加载 owner 的运行欠账。
def _cleanup_loaded_runtime_safety(agent) -> None:
    agents = [agent]
    pool = getattr(agent, "_owner_pool", None)
    if pool is not None:
        agents.extend(pool.active_agents())
    seen = set()
    for item in agents:
        repo = getattr(getattr(item, "subagents", None), "runtime_db", None)
        cleanup = getattr(repo, "cleanup_stale_runtime", None)
        if callable(cleanup) and id(repo) not in seen:
            seen.add(id(repo))
            try:
                cleanup()
            except Exception as exc:
                logging.getLogger(__name__).warning("owner runtime cleanup failed: %s", type(exc).__name__)


# LLM: CLI只解析可信owner归属并传宿主配置；三态/urgent和状态文件解释归memory_api，解析失败保unknown。
# 函数用途: 将Gateway候选身份适配到记忆领域只读预检，不创建冷owner Agent。
def _cold_curator_precheck(supervisor, owner) -> tuple[str, bool]:
    from ..agent.memory_api import cold_owner_curator_precheck

    try:
        base = supervisor._base_agent
        home = resolve_owner_home(base.home_paths.root, owner).home_dir
        return cold_owner_curator_precheck(home, config=base.config,
            curator_config_provider=lambda: getattr(getattr(base, "memory_curator", None), "config", None))
    except (OSError, ValueError, TypeError, AttributeError):
        return "unknown", False


# LLM: GatewayRunContext 拥有唯一长生命周期基础代理组合根，基础所有者控制器及请求车道均须复用它；所有者隔离仍由OwnerScopedAgentPool负责。
# 函数用途: 取得 Gateway 启动时已经初始化好的基础智能体，禁止后台线程再次加载配置并构造整套智能体。
def _gateway_agent_from_context(context: GatewayRunContext) -> SimpleAgent:
    agent = context.agent
    if agent is None:
        raise RuntimeError("GatewayRunContext 缺少已初始化的 agent")
    return agent


# LLM: 派发线程主体。tick 里任何 Exception（含错误打印本身失败）都由 _guarded_dispatch_tick 接住，线程只随 stop_event
#   退出；派发者构造/关闭失败与 BaseException 逃逸都经 _record_request_loop_exit 留下结构化退出事实（loop_health +
#   打印 + gateway 事件），Exception 按原语义吞掉返回，BaseException 原样上抛。2026-09-28 生产事故：磁盘写满时 print 抛
#   OSError 逃出 tick 的 except 块，while 外没有兜底，派发线程静默退出，pending 堆了一下午没人处理。改动须同步
#   test_gateway_dispatcher_resilience.py。
# LLM: cold C只接原control/Root/P3元数据泵，保原pending盖章和规范终态释放；不造Agent或运行owner恢复/输入对账，healthy及Count0主体保持。
#   首tick前准备无信号私有诊断；主线程后续同根安装幂等，不会撤掉首tick已挂的C看门狗。
# 函数用途: Count0及healthy C沿原派发池，cold C仅推进元数据；起停/错误记账保持，不构造虚拟业务Agent。
def _gateway_request_loop(context: GatewayRunContext, paths: GatewayPaths, stop_event: threading.Event) -> None:
    """请求处理循环:单派发者扫描 pending,按两层限流(每用户小坑+全局大坑)认领,
    交给按需扩张的执行线程池跑(池上限=全局大坑)。取代原「N 个 worker 线程各自扫描」
    的单层总闸——那个 N 就是旧的全局总 10。"""
    try:
        hang_diagnostics.install(paths.root, register_signal=False)
    except Exception as exc:
        _print_gateway_loop_error("gateway.diagnostics.prepare", "dispatcher", exc)
    loop_health.mark_started()
    stage = "gateway_request_pool.initialize"
    try:
        from ..agent.gateway_parts.multiprocess_background_discovery import (
            coordinator_background_required,
        )

        if coordinator_background_required(getattr(context, "control_plane", None), paths,
                getattr(context, "coordinator_worker_count", 0)):
            _require_coordinator_loop_source(context)
            if context.agent is None:
                from .gateway_coordinator_request_pump import CoordinatorMetadataRequestPump

                dispatcher = CoordinatorMetadataRequestPump(context, paths)
                stage = "gateway_control_admission.run"
                _run_request_dispatch(dispatcher, stop_event)
                dispatcher.shutdown()
                _record_request_loop_exit(context, "gateway_control_admission.shutdown", None)
                return
        dispatcher = _RequestDispatcher(context, paths)
        stage = "gateway_request_loop.run"
        _run_request_dispatch(dispatcher, stop_event)
        stage = "gateway_request_pool.shutdown"
        dispatcher.shutdown()
    except Exception as exc:
        _record_request_loop_exit(context, stage, exc)
        return
    except BaseException as exc:
        _record_request_loop_exit(context, stage, exc)
        raise
    _record_request_loop_exit(context, stage, None)


# LLM: 主循环每拍走守卫版 tick：派发到东西就立刻下一拍，空闲按轮询间隔等，连续出错按 LoopErrorBackoff 退避
#   （0.2 秒翻倍封顶 30 秒，成功清零），避免持续出错时每 0.2 秒打一条日志把磁盘再写满。关闭执行池由调用方做。
# 函数用途: 跑派发主循环直到 stop_event 置位。
def _run_request_dispatch(dispatcher: _RequestDispatcher, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        wait_seconds = _guarded_dispatch_tick(dispatcher)
        if wait_seconds > 0:
            stop_event.wait(wait_seconds)


# LLM: tick 外层守卫：接住从 tick 逃逸的任何 Exception（段外代码抛），次数记入 loop_health，打印按 dispatcher.backoff 限流
#   （同种错误第 1 次打、之后每 10 次打 1 次），打印本身不会再抛。返回下一拍该等多久：派发到东西 0；整拍空闲且派发段没失败
#   → 轮询间隔并清零退避；逃逸异常或派发段失败且没派发 → max(轮询间隔, 退避)。后台修复段（恢复/终态投影/输入回执调和）
#   的失败不在这里退避，它们只推迟各自的下次到期，派发轮询保持 0.2 秒。每次 tick 起止都记账本。
#   看门狗只包本次tick，finally撤销；不把worker里的长用户回合或退避等待误当派发挂起。
# 函数用途: 跑一次派发 tick，返回下一拍前要等的秒数。
def _guarded_dispatch_tick(dispatcher: _RequestDispatcher) -> float:
    loop_health.mark_tick_started()
    site = _LoopSite("dispatcher", "gateway_request_dispatch.tick", "dispatcher", GATEWAY_REQUEST_POLL_INTERVAL_SECONDS)
    _dispatch_stall_watchdog(DISPATCHER_STALL_TIMEOUT_SECONDS)
    try:
        dispatched = dispatcher.tick()
    except Exception as exc:
        _note_loop_tick_error(site, exc, dispatcher.backoff)
        loop_health.mark_tick_finished(0)
        return _wait_after_loop_error(dispatcher.backoff, site.interval)
    finally:
        _dispatch_stall_watchdog(0)
    loop_health.mark_tick_finished(dispatched)
    if dispatched > 0:
        return 0.0
    if dispatcher.tick_failed:
        return _wait_after_loop_error(dispatcher.backoff, site.interval)
    dispatcher.backoff.record_success()
    return site.interval


# LLM: 诊断故障不能改变tick、退避或原BaseException；模块持有fd时才操作C定时器。
# 函数用途: 尽力武装或撤销派发看门狗，异常仅报告。
def _dispatch_stall_watchdog(seconds: float) -> None:
    try:
        if seconds > 0:
            hang_diagnostics.arm_stall_watchdog(seconds)
        else:
            hang_diagnostics.disarm_stall_watchdog()
    except Exception as exc:
        _print_gateway_loop_error("gateway.dispatcher.watchdog", "dispatcher", exc)


# LLM: 一个后台循环的错误落点：loop 是 loop_health 里的计数键，context 是错误报告上下文，worker_id 是打印里的执行者，
#   interval 是该循环的正常节奏（只在 _run_loop_with_backoff / 守卫里用）。冻结，只当参数包，不含状态。
# 类用途: 描述"哪个循环、哪一段、以谁的名义打印、正常间隔多久"。
@dataclass(frozen=True)
class _LoopSite:
    loop: str
    context: str
    worker_id: str
    interval: float = 0.0


# LLM: 出错后的等待：连续出错按 LoopErrorBackoff 翻倍封顶 30 秒，但永远不比循环自己的正常间隔更快——退避只能放慢，不能把
#   60 秒一次的维护循环变成 0.2 秒重试。
# 函数用途: 算出错后下一拍前要等的秒数。
def _wait_after_loop_error(backoff: LoopErrorBackoff, interval: float) -> float:
    return max(float(interval), backoff.delay())


# LLM: 所有后台循环共用的出错记账：loop_health 按 loop id 记每一次，backoff 决定这次打不打印，打印本身不会抛。
# 函数用途: 记一次被守卫接住的异常，并按限流决定是否打印。
def _note_loop_tick_error(site: _LoopSite, exc: BaseException, backoff: LoopErrorBackoff) -> None:
    loop_health.note_loop_error(site.loop, site.context, exc)
    if backoff.record_failure(f"{site.context}:{type(exc).__name__}"):
        _print_gateway_loop_error(site.context, site.worker_id, exc)


# 派发 tick 里五个段的落点：计数都在 loop "dispatcher" 下，打印各以自己的 worker_id 出现。
_DISPATCH_SEGMENT_SITE = _LoopSite("dispatcher", "gateway_request_dispatch.iteration", "dispatcher")
_RECOVERY_SITE = _LoopSite("dispatcher", "gateway_request_recovery.iteration", "recovery")
_PROJECTION_SITE = _LoopSite("dispatcher", "gateway_terminal_projection.iteration", "terminal-projector")
_RECONCILE_SITE = _LoopSite("dispatcher", "gateway_input_reconcile.iteration", "input-reconciler")
# 锁 sidecar 清扫节拍：孤儿锁宽限 24 小时（gateway_parts.io._ORPHAN_LOCK_MIN_AGE_SECONDS），每小时扫一遍目录就够，不必跟着秒级 tick 跑。
_LOCK_SIDECAR_CLEANUP_INTERVAL_SECONDS = 3600.0
_LOCK_SIDECAR_CLEANUP_SITE = _LoopSite("dispatcher", "gateway_lock_sidecar_cleanup.iteration", "lock-sidecar-cleanup")


# LLM: 单一 tick 的后台循环（维护、调度器到期、孤儿恢复）统一节奏：成功等 interval 并清零退避，出错记账、限流打印、
#   等 max(interval, 退避)；异常绝不逃出，循环只随 stop_event 退出。新增后台循环必须走这里，不要自己 while+sleep。
# 函数用途: 按统一的退避/限流节奏跑一个后台循环直到 stop_event 置位。
def _run_loop_with_backoff(stop_event: threading.Event, tick: Callable[[], object], site: _LoopSite) -> None:
    backoff = LoopErrorBackoff()
    while not stop_event.is_set():
        try:
            tick()
        except Exception as exc:
            _note_loop_tick_error(site, exc, backoff)
            stop_event.wait(_wait_after_loop_error(backoff, site.interval))
            continue
        backoff.record_success()
        stop_event.wait(site.interval)


# LLM: 退出事实三处落：loop_health（内存，/status 与心跳可见）、错误打印（不会抛）、gateway 事件（log_gateway_event
#   自吞异常）。正常停止只记账本，不打印不发事件。
# 函数用途: 派发线程退出时记录结构化事实，stage 说明退在构造、运行还是关闭阶段。
def _record_request_loop_exit(context: GatewayRunContext, stage: str, error: BaseException | None) -> None:
    loop_health.mark_exited(error, stage)
    if error is None:
        return
    _print_gateway_loop_error(stage, "dispatcher", error)
    log_gateway_event(
        context.agent,
        "gateway_request_loop_exited",
        {"status": "exited", "pid": os.getpid(), "stage": stage, **loop_health.snapshot()},
    )


# LLM: 复用原执行池/分页恢复/公平扫描；C的实际gate、claim、线程和P6观察只来自本代组合根，Count0不创建MP来源。
# 类用途: 把原队列交给同一基础Agent执行，并独立修复历史投影；不持文件门等待线程。
class _RequestDispatcher:
    """两层限流派发者:admission(request_worker.admission)→ claim → 提交执行池。

    所有执行线程复用 GatewayRunContext 的 owner 级 agent；每轮可变字段由
    ThreadLocalAgentAttribute 隔离，远程用户/群继续由 OwnerScopedAgentPool 分流。
    不再给每个执行线程重复构造 backend、工具表、记忆和 capability cache。
    stale lease 恢复扫描随派发节流跑(原 worker-0 职责收进派发者)。"""

    # LLM: 原C正count组合根在pool ctor前供P6 initializer，真实worker进入业务前保Thread；默认0的None不新建观察/权威。
    # LLM: 原submit Future在本实例局部保留，退休未结不能随业务线程返回丢弃；不是第二队列或审批表。
    # LLM: 单进程准入绑定原bootstrap Agent的实时池预检，不冻结池快照、不创建业务实例；C仍沿原持久准入门。
    # 函数用途: 构造原池并保存原Future完成核收责任及本代线程观察。
    def __init__(self, context: GatewayRunContext, paths: GatewayPaths) -> None:
        self.context = context
        self.paths = paths
        self.bootstrap_agent = _gateway_agent_from_context(context)
        _attach_shared_owner_registry(self.bootstrap_agent, context)
        self.limits = AdmissionLimits.from_config(self.bootstrap_agent.config, agent=self.bootstrap_agent)
        # The request file and lease are authoritative.  If a provider/tool
        # call wedges during process shutdown, the next Gateway reclaims that
        # exact request; the old attempt cannot be allowed to hold Python open.
        self._drain_runtime = getattr(context, 'drain_runtime', None)
        self._drain_group = (self._drain_runtime.prepare_pool('foreground', 'gateway/request-pool')
                             if self._drain_runtime is not None else None)
        self._executor = DurableDaemonThreadPoolExecutor(
            max_workers=self.limits.global_inflight, thread_name_prefix="gw-exec",
            initializer=self._drain_group.remember_current_thread if self._drain_group is not None else None,
        )
        if self._drain_group is not None:
            self._drain_group.bind_executor(self._executor)
        self._request_futures = {}
        self._future_gate = threading.Lock()
        self._scan_gate = GatewayInboxScanGate()
        self._recover_throttle = _RecoverThrottle(self.bootstrap_agent)
        # LLM: 内部多进程模式（count>0）由 C 的持久准入门接管派发准入（预留+盖章）；默认单进程
        #   不 import 该模块、不读状态、不建目录，进程内两层限流与线程返回释放一字不变。
        self._admission_gate = None
        if int(getattr(context, "coordinator_worker_count", 0) or 0) > 0:
            from ..agent.gateway_parts.multiprocess_admission import coordinator_admission_gate

            self._admission_gate = coordinator_admission_gate(self.bootstrap_agent, self.paths, context)
            if self._drain_runtime is not None:
                self._drain_runtime.retain_admission(self._admission_gate)
        self._next_input_reconcile_at = 0.0
        self._next_terminal_projection_at = 0.0
        # 启动后的第一次终态投影扫描走全量（崩溃恢复保证）；之后按目录时间戳节流。
        self._terminal_projection_startup = True
        # 派发段失败与逃逸异常共用一份循环退避；三个后台修复段各自一份，失败只推迟自己的下次到期，绝不拖慢派发轮询
        #（tick 的约定：Historical projection and reconciliation scans must never sit in front of an interactive request）。
        self.backoff = LoopErrorBackoff()
        self.tick_failed = False
        self._recovery_backoff = LoopErrorBackoff()
        self._projection_backoff = LoopErrorBackoff()
        self._reconcile_backoff = LoopErrorBackoff()
        self._lock_cleanup_backoff = LoopErrorBackoff()
        self._next_lock_cleanup_at = 0.0


    # LLM: 原P6已停止出生则不再认领；Count0/原组件缺Root只读内存默认None，安全重启仍按原结构化等待，原T/P3/恢复与终态投影不变。
    # 恢复、终态投影、输入回执调和及孤儿锁清扫进入前调用note_dispatch_tick_progress刷新单调进展。改动须同步 test_gateway_safe_restart.py。
    # 函数用途: 派发一轮待处理请求并执行节流的恢复/投影/调和扫描，返回本轮派发数量。
    def tick(self) -> int:
        # New work gets the first bounded slice. Historical projection and
        # reconciliation scans must never sit in front of an interactive request.
        self._collect_request_futures()
        self.tick_failed = False
        runtime = getattr(self, '_drain_runtime', None)
        if runtime is not None and runtime._closing:
            return 0
        dispatched = 0
        try:
            # mc4-1704：派发前先回收"已有规范终态但仍 occupied"的预留，崩溃/重启遗留名额不堵新请求。
            if self._admission_gate is not None:
                self._admission_gate.release_settled()
            dispatched = dispatch_pending_requests(
                self.paths,
                self.limits,
                self._submit,
                scan_gate=self._scan_gate,
                hold_reason=RESTART_DRAIN_HOLD_REASON if restart_draining() else "",
                admission_gate=self._admission_gate,
            )
        except Exception as exc:
            # 只有派发段自己失败才让外层守卫退避（没请求可派时）；账本记在 loop "dispatcher" 下，打印按循环退避限流。
            self.tick_failed = True
            _note_loop_tick_error(_DISPATCH_SEGMENT_SITE, exc, self.backoff)
        # Recovery and projections are independent durability domains. One
        # damaged receipt must not starve normal jobs or another repair domain.
        if self._recover_throttle.due():
            self._note_segment_progress()
            self._recover_throttle.defer(self._run_segment(_RECOVERY_SITE, self._recovery_backoff, self._recover_once))
        now = time.monotonic()
        if now >= self._next_terminal_projection_at:
            self._note_segment_progress()
            delay = self._run_segment(_PROJECTION_SITE, self._projection_backoff, self._project_terminals_once)
            self._next_terminal_projection_at = now + max(0.75, delay)
        if now >= self._next_input_reconcile_at:
            self._note_segment_progress()
            delay = self._run_segment(_RECONCILE_SITE, self._reconcile_backoff, self._reconcile_inputs_once)
            self._next_input_reconcile_at = now + max(0.75, delay)
        if now >= self._next_lock_cleanup_at:
            self._note_segment_progress()
            delay = self._run_segment(
                _LOCK_SIDECAR_CLEANUP_SITE, self._lock_cleanup_backoff, self._cleanup_lock_sidecars_once
            )
            self._next_lock_cleanup_at = now + max(_LOCK_SIDECAR_CLEANUP_INTERVAL_SECONDS, delay)
        return dispatched


    # LLM: 相邻维护段切换时同步刷新loop_health在途单调钟与faulthandler看门狗预算；裸调tick不在途时不武装看门狗。
    # 函数用途: 在进入下一维护段前重置单段卡死超时预算。
    def _note_segment_progress(self) -> None:
        if loop_health.note_dispatch_tick_progress():
            _dispatch_stall_watchdog(DISPATCHER_STALL_TIMEOUT_SECONDS)


    # LLM: 后台修复段的统一跑法：成功清零该段退避并返回 0；失败记账（loop "dispatcher"）、按该段自己的退避限流打印，
    #   返回该段应额外推迟的秒数——只推迟这一段的下次到期，不碰派发轮询，也不置 tick_failed。
    # 函数用途: 跑一个后台修复段，返回它下次到期要额外等的秒数。
    def _run_segment(self, site: _LoopSite, backoff: LoopErrorBackoff, operation: Callable[[], object]) -> float:
        try:
            operation()
        except Exception as exc:
            _note_loop_tick_error(site, exc, backoff)
            return backoff.delay()
        backoff.record_success()
        return 0.0

    # LLM: 正MP只传本dispatcher原P3门；实际P2长观察由恢复链在T外核，旧域/Runtime未知不由TTL授恢复权。
    # 函数用途: 用本代真实准入源扫描恢复旧processing；Count0仍不创建gate或新执行账。
    def _recover_once(self) -> None:
        if self._admission_gate is None:
            recover_gateway_processing_requests(
                self.paths,
                startup=False,
                max_attempts=self.bootstrap_agent.config.gateway_request_max_attempts,
                timeout_seconds=self.bootstrap_agent.config.gateway_processing_timeout_seconds,
                agent=self.bootstrap_agent,
            )
            return
        from ..agent.gateway_parts.recovery import _non_negative_int, _RecoveryContext

        config = self.bootstrap_agent.config
        params = _RecoveryContext(
            time.time(), _non_negative_int(config.gateway_request_max_attempts, default=1),
            max(1, int(config.gateway_processing_timeout_seconds or 1)), False, self.bootstrap_agent, None,
            admission_gate=self._admission_gate,
        )
        recover_gateway_processing_requests(self.paths, params=params)


    # LLM: 终态post只能复用本dispatcher的原gate重新核Runtime/owner政策；Count0无来源查询，原分页/追平合同不变。
    # 函数用途: 用本代准入来源补原终态欠账，启动完整核对后沿原扫描游标低频重核。
    def _project_terminals_once(self) -> None:
        startup, self._terminal_projection_startup = self._terminal_projection_startup, False
        recovery_context = None
        if self._admission_gate is not None:
            from ..agent.gateway_parts.recovery import _RecoveryContext

            recovery_context = _RecoveryContext(
                time.time(), 1, 1, False, self.bootstrap_agent, None,
                admission_gate=self._admission_gate,
            )
        repair_gateway_terminal_projections(
            self.paths, agent=self.bootstrap_agent, limit=16, full_scan=startup,
            recovery_context=recovery_context,
        )

    # LLM: 对账结果里的错误（对账抛错、带错误的终态未知）要抛给段守卫：进 loop_health 计数与限流打印，并只推迟本段自己的
    #   下次到期；不能像以前那样把 summary 丢掉让坏账回执被静默重读。
    #   正C只把当前context/Root/gate交queued消费者，bound明确延期；Count0仍原完整对账，不读新角色或库。
    # 函数用途: 输入调和段沿本代实际来源执行，失败或未支持状态参与原独立退避。
    def _reconcile_inputs_once(self) -> None:
        gate, context = getattr(self, '_admission_gate', None), getattr(self, 'context', None)
        if (gate is not None or getattr(context, 'control_plane', None) is not None
                or getattr(context, 'drain_runtime', None) is not None
                or int(getattr(context, 'coordinator_worker_count', 0) or 0) > 0):
            from ..agent.gateway_parts.multiprocess_input_reconcile import (
                reconcile_coordinator_queued_inputs,
                require_coordinator_input_progress,
            )

            require_coordinator_input_progress(
                reconcile_coordinator_queued_inputs(context, self.paths, gate)
            )
            return
        raise_if_input_reconcile_unsettled(
            reconcile_gateway_input_receipts(self.paths, self.bootstrap_agent, limit=64)
        )

    # LLM: 锁 sidecar 清扫段：只删数据文件已不存在、超过 24 小时宽限、且非阻塞拿得到 flock 的 <name>.lock；
    #   活锁、数据文件还在、宽限内的一律不动；单文件失败在 cleanup_orphan_lock_files 内只跳过。
    # 函数用途: 清理请求目录里累积的孤儿锁文件，失败不拖垮其它段。
    def _cleanup_lock_sidecars_once(self) -> None:
        for directory in gateway_lock_sidecar_dirs(self.paths):
            cleanup_orphan_lock_files(directory)

    # LLM: 已认领原claim提交失败保持原处理责任；P6保未知出生缺口，不把空_threads当不存在，Count0不增观察。
    # LLM: 保存真实submit返回的原Future与原claim；不重新提交错误或复制执行source。
    # 函数用途: 提交原请求并保留准确完成对象，提交失败仍沿原资源责任。
    def _submit(self, claim: GatewayClaim, user_key: str, conversation_key: str) -> None:
        try:
            future = self._executor.submit(self._execute, claim, user_key, conversation_key)
            with self._future_gate:
                self._request_futures[future] = (claim, user_key, conversation_key)
        except BaseException as error:
            if self._drain_group is not None:
                self._drain_group.retain_fault(error)
            raise


    # LLM: 冻结C上下文覆盖正常执行和异常终结；Count0沿原初始化失败收口，不取C来源或提前依赖bootstrap对象；finally不释放持久预留。
    # LLM: 原native或携其债务的outer链同对象交原Future，不能吞成普通terminalize；查链不授终态，原计数释放不同于持久P3释放。
    # 函数用途: 执行准确原claim，实际未结由原Future核收，不重新选择owner。
    def _execute(self, claim: GatewayClaim, user_key: str, conversation_key: str) -> None:
        from ..agent.observability.concurrency_metrics import gateway_worker_busy

        agent = None
        processing_path = claim.path
        gateway_worker_busy(1)
        try:
            scope = nullcontext()
            if self._admission_gate is not None:
                from ..agent.gateway_parts.multiprocess_coordinator_execution import (
                    coordinator_execution_scope,
                )

                scope = coordinator_execution_scope(self._admission_gate, claim, self.bootstrap_agent)
            with scope:
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
                    from ..agent.conversation.agent_tool_approval import (
                        find_approval_retirement_error,
                    )

                    if find_approval_retirement_error(exc) is not None:
                        raise
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


    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 取得当前维护线程绑定的Agent，保留owner隔离实例。
    def _thread_agent(self) -> SimpleAgent:
        return self.bootstrap_agent

    # LLM: 只关闭原executor出生；原native异常链也须保原outer对象，shutdown不等文件锁，不以线程池退出代替业务终态核收。
    # 函数用途: 停止原请求池并核收已经返回的义务，不以线程池shutdown冒充清理完成。
    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
        self._collect_request_futures()
        with self._future_gate:
            pending = tuple(self._request_futures)
        from ..agent.conversation.agent_tool_approval import find_approval_retirement_error

        for future in pending:
            if future.done() and find_approval_retirement_error(future.exception()) is not None:
                raise future.exception()

    # LLM: dict只持原submit Future/claim，不新建任务；锁内仅snapshot/pop，permission try与canonical只读均在锁外。
    # 函数用途: 每轮只try原负向清理；准确canonical终态、旧attempt与claim俱已接收才移除，外层/物理未知仍保Future。
    def _collect_request_futures(self):
        from ..agent.conversation.agent_tool_approval import (
            complete_approval_retirement_future,
            find_approval_retirement_error,
        )

        with self._future_gate:
            rows = tuple(self._request_futures.items())
        for future, work in rows:
            if not future.done():
                continue
            error = future.exception()
            if find_approval_retirement_error(error) is not None and not complete_approval_retirement_future(error, work[0]):
                continue
            with self._future_gate:
                if self._request_futures.get(future) is work:
                    del self._request_futures[future]


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 把共享活跃owner登记表绑定到隔离Agent，不复制其它owner业务状态。
def _attach_shared_owner_registry(agent: SimpleAgent, context: GatewayRunContext) -> None:
    """把共享活跃 owner 登记表(挂在 context.agent 上)同步给本 worker 的隔离 agent。best-effort。"""
    try:
        agent._active_owner_registry = shared_active_owner_registry(context.agent)
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_worker.owner_registry", "owner-registry", exc)


# LLM: C信号仅拒旧inline回落；control/Root/lease/runtime仍须同原对象、PID/FD和冻结代次，缺失/复制/关闭不得从DTO重建许可。
# 函数用途: 验证当前冷C循环确属本次真实组合根，不发模型、建线程或写业务文件。
def _require_coordinator_loop_source(context):
    from ..agent.gateway_parts.control_plane import GatewayControlPlane
    from ..agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime

    control, runtime = getattr(context, "control_plane", None), getattr(context, "drain_runtime", None)
    if (type(control) is not GatewayControlPlane or type(runtime) is not GatewayDrainRuntime
            or control.paths is not context.paths or runtime.control is not control
            or type(context.coordinator_worker_count) is not int
            or context.coordinator_worker_count != control.worker_count):
        raise ValueError("gateway_original_coordinator_loop_required")
    runtime.require_original()
    controller = control.controller()
    if runtime._closing or controller is None or not controller.active():
        raise ValueError("gateway_original_coordinator_loop_closed")


# LLM: Count0原supervisor/三设施线程保序；C只接真实metadata来源，不物化远端owner Agent或执行旧inline维护，坏源不回退。
# 函数用途: 依本次真实组合根选择既有业务循环或C来源发现循环。
def _gateway_background_main_loop(context: GatewayRunContext, stop_event: threading.Event) -> None:
    from ..agent.gateway_parts.multiprocess_background_discovery import (
        coordinator_background_required,
    )

    if coordinator_background_required(getattr(context, "control_plane", None), getattr(context, "paths", None),
            getattr(context, "coordinator_worker_count", 0)):
        _run_coordinator_background_sources(context, stop_event)
        return
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



# LLM: 只复用原C发现producer的真实control/Root/lease；普通tick退避保原LoopErrorBackoff，退出不重放、释放P3或启动业务Agent。
# 函数用途: 在同一原后台线程推进metadata来源发现，不启动三条旧业务控制线程。
def _run_coordinator_background_sources(context, stop_event):
    from ..agent.gateway_parts.multiprocess_background_discovery import (
        CoordinatorBackgroundRequests,
    )

    _require_coordinator_loop_source(context)
    sources = CoordinatorBackgroundRequests(context.control_plane, drain_runtime=context.drain_runtime)
    backoff = LoopErrorBackoff()
    site = _LoopSite("background-main", "gateway_background_main.sources", "background-main", _background_main_poll_interval())
    while not stop_event.is_set():
        wait_seconds = _supervisor_tick_survives(sources, backoff, site)
        if wait_seconds > 0:
            stop_event.wait(wait_seconds)


# LLM: 后台主循环节奏与派发循环同一套：有活立刻下一拍，空闲等 poll_interval，连续出错按 LoopErrorBackoff 退避并限流打印。
# 函数用途: 跑后台主循环直到 stop_event 置位。
def _run_background_main_ticks(
    supervisor: object,
    stop_event: threading.Event,
    poll_interval: float,
) -> None:
    backoff = LoopErrorBackoff()
    site = _LoopSite("background-main", "gateway_background_main.tick", "background-main", poll_interval)
    while not stop_event.is_set():
        wait_seconds = _supervisor_tick_survives(supervisor, backoff, site)
        if wait_seconds > 0:
            stop_event.wait(wait_seconds)


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 按原停止事件关闭Gateway后台主循环及其资源。
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


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 启动不依赖模型的孤儿核对线程，遵守原停止事件。
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


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 启动owner维护线程，沿原节拍和停止机制运行。
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


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 启动定时任务到期检查线程，不改任务授权或执行身份。
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


# LLM: 返回下一拍前要等的秒数：有活 0、空闲 site.interval、出错 max(interval, 退避)（连续出错翻倍封顶 30 秒，打印限流）。
#   异常只记账、按限流打印，绝不逃出；循环只随 stop_event 退出。
# 函数用途: 跑一次后台主循环 tick，返回下一拍前要等的秒数。
def _supervisor_tick_survives(
    supervisor: _BackgroundMainSupervisor, backoff: LoopErrorBackoff, site: _LoopSite
) -> float:
    """永不停机硬保障:tick 的编排缝隙(种子重扫/owner 池同步/提交/汇报)不在 _safe_tick
    保护内,曾能把后台主循环线程整个杀死——网关被 systemd 拉着 active,干活的循环却再也
    不回来(真机 429 断供实锤的死法之一)。任何异常打点后退避一拍继续,循环只随
    stop_event 退出。"""
    try:
        busy = bool(supervisor.tick())
    except Exception as exc:
        _note_loop_tick_error(site, exc, backoff)
        return _wait_after_loop_error(backoff, site.interval)
    backoff.record_success()
    return 0.0 if busy else site.interval


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 按当前宿主依赖构造后台调度器，不直接执行模型任务。
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
_BACKGROUND_OWNER_WORKER_COUNT = 8
# 同 owner 后台会话并发上限：一条长会话不再占住整个 owner，但也不允许
# 一个 owner 用大量待处理会话占满全局后台模型池。
_BACKGROUND_PER_OWNER_THREAD_COUNT = 4
# 记忆策展并发 worker 数 2
_MEMORY_CURATOR_WORKER_COUNT = 2
_BASE_SCHEDULER_KEY = "base"
# 调度器存活心跳节流(秒):>5 分钟没更新即疑死,配合 systemd 快速定位。
_HEARTBEAT_INTERVAL_SECONDS = 60
# 策展每日全局配额(默认 5000 次/天,config curator_daily_quota 覆盖):LLM 提取烧钱,
# 归档类晚一天无害,超限顺延;紧急车道(pending reason)不受此限。
_CURATOR_DAILY_QUOTA_COUNT = 5000
# 算实际消耗的 run 结果状态:只有真正执行了一次 curator 事务才记账;not_due(interval
# 未到/无新消息)、busy(他人持 lease)、disabled 都没碰 LLM,不算消耗。
_CURATOR_CONSUMED_STATUSES = frozenset({"succeeded", "failed"})


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 读取owner记忆总开关，关闭时不调度Curator。
def _agent_memory_enabled(agent: object) -> bool:
    """owner memory_policy 总闸(effective flag):关闭则 curator 不调度。

    拿不到 effective policy 的路径(测试桩/旧装配)视为开启——与「memory_policy.json
    缺失视为开启」的默认语义一致,短路只发生在显式 enabled=false 时。"""
    policy = getattr(agent, "owner_policy", None)
    if policy is None:
        return True
    return bool(getattr(policy, "memory_enabled", True))


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 读取后台owner工作线程配置，沿原有效范围和默认值。
def _background_owner_workers(agent: object) -> int:
    value = getattr(getattr(agent, "config", None), "background_owner_workers", _BACKGROUND_OWNER_WORKER_COUNT)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _BACKGROUND_OWNER_WORKER_COUNT
    return parsed if parsed > 0 else _BACKGROUND_OWNER_WORKER_COUNT


# LLM: 此配置上限限制同一身份下不同持久会话线程同时运行的模型回合；零或非法值使用安全默认值。
# 函数用途: 读取单个用户后台会话的最大并发数。
def _background_threads_per_owner(agent: object) -> int:
    value = getattr(
        getattr(agent, "config", None),
        "background_threads_per_owner",
        _BACKGROUND_PER_OWNER_THREAD_COUNT,
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _BACKGROUND_PER_OWNER_THREAD_COUNT
    return parsed if parsed > 0 else _BACKGROUND_PER_OWNER_THREAD_COUNT


# LLM: 记忆提取使用独立有界车道，防止低优先级所有者积压占满会话唤醒执行器；非法值保留安全默认值。
# 函数用途: 读取单 Gateway 同时运行的记忆策展数量，超出的 owner 留待下一轮。
def _memory_curator_workers(agent: object) -> int:
    value = getattr(
        getattr(agent, "config", None),
        "memory_curator_workers",
        _MEMORY_CURATOR_WORKER_COUNT,
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _MEMORY_CURATOR_WORKER_COUNT
    return parsed if parsed > 0 else _MEMORY_CURATOR_WORKER_COUNT


# LLM: 此混入类只拥有进程内规划及执行槽位；持久会话来源和运行认领仍属于BackgroundMainAgentScheduler及存储层。
# 类用途: 为单 Gateway 按 owner 公平提交独立的 thread 后台车道。
class _BackgroundOwnerPlannerMixin:
    # LLM: 只读取原store/数据库已声明路径的stat和登记本活动代次；未知或拒读时禁用退避，不扫描正文或创建新权威。
    # 函数用途: 发现新唤醒、请求、策略与数据库写入，使空闲owner在下一拍立即恢复规划。
    def _owner_planner_signature(self, scheduler: object) -> tuple | None:
        store = getattr(scheduler, "store", None)
        agent = getattr(getattr(scheduler, "runtime", None), "agent", None)
        paths = [getattr(store, name, None) for name in (
            "wake_queue_dir", "policies_dir", "tasks_dir", "goals_dir", "observations_dir",
            "guidance_dir", "session_tasks_dir", "background_claims_dir",
        )]
        for repo in (getattr(getattr(agent, "subagents", None), "runtime_db", None), getattr(agent, "store", None)):
            db_path = getattr(repo, "db_path", None)
            if db_path is not None:
                paths.extend((db_path, str(db_path) + "-wal"))
        service_repo = getattr(getattr(scheduler, "scheduler_service", None), "repository", None)
        paths.append(getattr(service_repo, "store_path", None))
        collaboration = getattr(scheduler, "collaboration_store", None)
        paths.append(getattr(collaboration, "cases_dir", None))
        signatures = []
        for path in paths:
            if path is None:
                continue
            try:
                stat = Path(path).stat()
                stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except FileNotFoundError:
                stamp = None
            except OSError:
                return None
            signatures.append((str(path), stamp))
        if not signatures:
            return None
        registry, activity = getattr(self, "_registry", None), 0.0
        if registry is not None and hasattr(registry, "_recorded_at"):
            owner = owner_identity_from_config(agent.config)
            with registry._lock:
                activity = registry._recorded_at.get((owner.provider, owner.owner_kind, owner.owner_id), 0.0)
        return tuple(signatures), activity

    # LLM: 0/非法配置或未知来源维持每拍规划；活动、新代次、失败重试先清零，表里仅保留旧身份不算新活动。
    # 函数用途: 决定这个owner能否等待下一次空闲规划截止点。
    def _owner_plan_backoff_state(self, key: object, scheduler: object, *, active: bool) -> tuple:
        streaks = self.__dict__.setdefault("_owner_idle_streaks", {})
        checks = self.__dict__.setdefault("_owner_next_check_at", {})
        signatures = self.__dict__.setdefault("_owner_plan_signatures", {})
        try:
            cap = float(getattr(self._base_agent.config, "background_idle_owner_max_backoff_seconds", 0.0))
        except (TypeError, ValueError):
            cap = 0.0
        cap = cap if math.isfinite(cap) and cap > 0 else 0.0
        signature = self._owner_planner_signature(scheduler) if cap else None
        changed = key in signatures and signatures[key] != signature
        reset = active or changed or not cap or signature is None or bool(self._lane_retry.count(str(key)))
        if reset:
            streaks[key], checks[key] = 0, 0.0
        wait = not reset and time.monotonic() < checks.get(key, 0.0)
        return wait, cap, signature, reset

    # LLM: 已知策略到期及尚未消费的wake不能被空闲退避盖住；只在空规划时沿原读取口核实，坏账立即重查。
    # 函数用途: 将指数退避限制在原有待办的最早可行动时间以内。
    def _owner_plan_idle_delay(self, scheduler: object, delay: float, planned_at: float) -> float:
        store = getattr(scheduler, "store", None)
        pending = getattr(getattr(store, "wakes", None), "pending", None)
        if callable(pending) and pending(limit=1):
            return 0.0
        report = getattr(getattr(store, "progress", None), "list_report", None)
        if callable(report):
            policies, errors = report(enabled_only=True)
            if errors:
                return 0.0
            for policy in policies:
                delay = min(delay, max(0.0, float(policy.next_due_at) - planned_at))
        return delay

    # LLM: 空规划只改变进程内下次检查时刻，不修改wake/claim/任务；有活动或新代次时保持零退避。
    # 函数用途: 记录本owner本拍结果并递增封顶的空闲等待。
    def _note_owner_plan(self, key: object, signature: tuple | None, delay: float, planned_mono: float) -> None:
        self._owner_plan_signatures[key] = signature
        next_at = planned_mono + delay
        if delay <= 0 or next_at <= time.monotonic():
            self._owner_idle_streaks[key], self._owner_next_check_at[key] = 0, 0.0
            return
        self._owner_idle_streaks[key] = min(5, self._owner_idle_streaks.get(key, 0) + 1)
        self._owner_next_check_at[key] = next_at

    # LLM: 只在真正规划的线程创建原作用域；关闭配置时清理旧表并显式禁用外层缓存，不改变后台执行连接。
    # 函数用途: 为一个owner激活跨拍复用的SQLite规划连接表。
    def _planner_sqlite_scope(self, owner_key: object):
        from ..agent.runtime_db.sqlite_scope import (
            activate_sqlite_scope,
            new_persistent_sqlite_scope,
        )

        scopes = self.__dict__.setdefault("_owner_planner_sqlite_scopes", {})
        used_at = self.__dict__.setdefault("_owner_planner_scope_used_at", {})
        if not bool(getattr(self._base_agent.config, "sqlite_connection_reuse", False)):
            self._drop_owner_planner_scope(owner_key)
            return activate_sqlite_scope(None)
        scope = scopes.get(owner_key)
        if scope is None or scope.closed:
            scope = new_persistent_sqlite_scope()
            scopes[owner_key] = scope
        used_at[owner_key] = time.monotonic()
        return activate_sqlite_scope(scope)

    # LLM: 只回收进程内规划表，保留原持久任务；关闭失败按原日志上报，不吞中断或改owner身份。
    # 函数用途: 在owner退休、禁用复用或停机时关闭其规划连接。
    def _drop_owner_planner_scope(self, owner_key: object) -> None:
        from ..agent.runtime_db.sqlite_scope import close_persistent_sqlite_scope

        scope = getattr(self, "_owner_planner_sqlite_scopes", {}).pop(owner_key, None)
        getattr(self, "_owner_planner_scope_used_at", {}).pop(owner_key, None)
        if scope is not None:
            close_persistent_sqlite_scope(scope)

    # LLM: 表只跟随当前owner调度器，60秒未使用即退役；逐owner收集错误后继续清理，最后传播首错。
    # 函数用途: 有界保留当前规划者并释放退出或长期未规划的owner连接。
    def _retain_owner_planner_scopes(self, owner_keys: set[object]) -> None:
        now = time.monotonic()
        used_at = getattr(self, "_owner_planner_scope_used_at", {})
        first_error = None
        for key in tuple(getattr(self, "_owner_planner_sqlite_scopes", {})):
            if key not in owner_keys or now - used_at.get(key, now) >= 60.0:
                try:
                    self._drop_owner_planner_scope(key)
                except BaseException as exc:
                    first_error = exc if first_error is None else first_error
        for name in ("_owner_idle_streaks", "_owner_next_check_at", "_owner_plan_signatures"):
            state = getattr(self, name, {})
            for key in tuple(state):
                if key not in owner_keys:
                    state.pop(key, None)
        if first_error is not None:
            raise first_error


# LLM: 仅持有本进程执行槽；规划缓存继承私有mixin，原scheduler/store仍是durable执行权威。
# 类用途: 公平提交owner/thread后台车道，不把规划连接传给执行线程。
class _BackgroundThreadLaneSupervisorMixin(_BackgroundOwnerPlannerMixin):
    # LLM: 规划单线程且不调用模型，提交按所有者轮转、同时受全局及逐所有者上限约束，并以持久线程身份为键。
    # 恢复扫描属于Gateway独立孤儿对账器，准备阶段必须跳过重复内联扫描，再提交就绪车道。
    # 每个owner的prepare/ready短查询按配置sqlite_connection_reuse跨拍复用连接；异常也解除Context，提交和后台执行不继承连接。
    # 函数用途: 复用规划连接后公平提交就绪会话；等待与暂停不占工位，单owner异常不影响其它窗口。
    def _submit_ready_thread_ticks(self) -> None:
        global_limit = _background_owner_workers(self._base_agent)
        available = max(0, global_limit - len(self._inflight))
        per_owner_limit = _background_threads_per_owner(self._base_agent)
        schedulers = [
            (_BASE_SCHEDULER_KEY, self._base_scheduler),
            *list(self._owner_schedulers.items()),
        ]
        self._retain_owner_planner_scopes({key for key, _scheduler in schedulers})
        if available <= 0:
            return
        self._lane_retry.retain_owners(str(key) for key, _scheduler in schedulers)
        candidates: list[tuple[object, BackgroundMainAgentScheduler, list[str]]] = []
        planned_mono = time.monotonic()
        planned_at = time.time()
        # 池内缓存驻留不代表执行；只有原pin计数才说明前台/后台正在租用这个实例。
        pool = getattr(self, "_owner_pool", None)
        active_agents = set()
        if pool is not None and hasattr(pool, "_pins"):
            with pool._lock:
                active_agents = {key for key, count in pool._pins.items() if count > 0}
        for owner_key, scheduler in schedulers:
            active_threads = self._active_threads_for_owner(owner_key)
            owner_slots = max(0, per_owner_limit - len(active_threads))
            if owner_slots <= 0:
                continue
            try:
                agent = getattr(getattr(scheduler, "runtime", None), "agent", None)
                active = bool(active_threads) or id(agent) in active_agents
                wait, cap, signature, reset = self._owner_plan_backoff_state(owner_key, scheduler, active=active)
                if wait:
                    continue
                with self._planner_sqlite_scope(owner_key):
                    scheduler.prepare_tick(
                        now=planned_at,
                        include_orphan_supervision=False,
                    )
                    ready = scheduler.ready_thread_ids(
                        now=planned_at,
                        limit=per_owner_limit + len(active_threads) + self._lane_retry.count(str(owner_key)),
                    )
                    streak = self._owner_idle_streaks.get(owner_key, 0) + 1
                    delay = min(cap, 0.5 * (2 ** min(streak, 5))) if not reset and not ready else 0.0
                    if delay:
                        delay = self._owner_plan_idle_delay(scheduler, delay, planned_at)
                self._note_owner_plan(owner_key, signature, delay, planned_mono)
            except Exception as exc:
                _print_gateway_loop_error(
                    "gateway_background_main.plan",
                    f"background-main:{owner_key}",
                    exc,
                )
                continue
            pending = [thread_id for thread_id in ready if thread_id not in active_threads
                       and self._thread_lane_can_retry(scheduler, str(owner_key), thread_id)]
            if pending:
                candidates.append((owner_key, scheduler, pending[:owner_slots]))
        self._submit_thread_candidates(candidates, available=available)

    # LLM: 配置与模型指纹只在失败车道复查（指纹只在环境暂停时读）；读取失败也局限于当前线程，不让一份坏配置阻塞同用户其它会话。
    # 函数用途: 检查一条后台车道是否可以再跑，不构造后端或占用模型执行工位。
    def _thread_lane_can_retry(self, scheduler: BackgroundMainAgentScheduler, label: str, thread_id: str) -> bool:
        try:
            return self._lane_retry.ready(
                label, thread_id,
                model_ready=lambda: thread_model_is_configured(scheduler.runtime.agent, thread_id),
                fingerprint=lambda: thread_model_fingerprint(scheduler.runtime.agent, thread_id),
            )
        except Exception as exc:
            self._record_thread_failure(label, thread_id, exc)
            return False

    # LLM: 执行异常与恢复检查异常共用 typed 退避及脱敏日志，不修改持久 wake、claim 或 Goal。
    # 函数用途: 记录精确失败车道和恢复条件，环境级故障按车道暂停，普通错误按代码常量 BACKGROUND_MAIN_ERROR_BACKOFF_SECONDS 冷却。
    def _record_thread_failure(self, label: str, thread_id: str, exc: Exception) -> None:
        self._lane_retry.failed(label, thread_id, exc, delay=BACKGROUND_MAIN_ERROR_BACKOFF_SECONDS)
        _print_gateway_loop_error(
            "gateway_background_main.iteration", f"background-main:{label}:{thread_id}", exc,
        )

    # LLM: 候选提交使用已有界执行器，工作者取得运行认领之前绝不改变持久来源状态。
    # LLM: 停机关闭同一进程内提交门；已入队但未进入的job仍由原观察入口拒绝。
    # 函数用途: 按 owner 轮询把候选会话填入剩余全局工位。
    def _submit_thread_candidates(
        self,
        candidates: list[tuple[object, BackgroundMainAgentScheduler, list[str]]],
        *,
        available: int,
    ) -> None:
        if self._shutdown_control.stopping:
            return
        executor = self._get_executor()
        while not self._shutdown_control.stopping and available > 0 and any(rows for _key, _scheduler, rows in candidates):
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

    # LLM: 在途身份由所有者与线程联合确定；只检查所有者会把独立TUI会话当成同一执行，重新引入饥饿问题。
    # 函数用途: 列出某 owner 正在跑的后台会话 ID。
    def _active_threads_for_owner(self, owner_key: object) -> set[str]:
        return {
            str(key[1])
            for key in self._inflight
            if isinstance(key, tuple) and len(key) == 2 and key[0] == owner_key
        }

    # LLM: 指标只统计真实会话模型车道，持久线程认领仍是跨进程防重复执行的最终边界。
    # LLM: 同一job携带本片停止观察，原claim后绑定；停止后不接新片，finally核原账而非另写终态。
    # 函数用途: 运行一条会话后台车道并更新并发指标。
    def _counted_thread_tick(
        self,
        scheduler: BackgroundMainAgentScheduler,
        label: str,
        thread_id: str,
    ) -> list[object]:
        background_tick_inflight(1)
        try:
            repo = getattr(getattr(scheduler.runtime.agent, "subagents", None), "runtime_db", None)
            with self._shutdown_control.observe(repo):
                return self._safe_thread_tick(scheduler, label, thread_id)
        except InterruptedError:
            return []
        finally:
            background_tick_inflight(-1)

    # LLM: 守护执行器必须严格遵守配置的全局上限。
    # 函数用途: 惰性创建全局有界后台会话线程池。
    def _get_executor(self) -> object:
        if self._executor is None:
            self._executor = DurableDaemonThreadPoolExecutor(
                max_workers=_background_owner_workers(self._base_agent),
                thread_name_prefix="bg-owner",
            )
        return self._executor

    # LLM: 关闭只取消两条车道中进程本地仍排队的提交；持久会话和记忆整理来源保持待处理，供重启恢复。
    # 关池之前先给本进程在途的唤醒尝试打停机标记（C6，mark_inflight_attempts_stopping 内部逐条吞掉写失败，不会让停机失败），
    # 下次 preflight 按不计数的 attempt:gateway_stopped 补记，部署重启不给在途唤醒计失败。
    # LLM: 主会话已运行future沿准确claim中断并共用两秒绝对预算核收，未退/未结必须交cleanup为incomplete。
    # 函数用途: 停止主会话与记忆策展线程池并关闭规划SQLite连接；不删除持久任务或记忆游标。
    def shutdown(self) -> None:
        from ..agent.conversation.wake_attempt_tracking import mark_inflight_attempts_stopping
        from ..agent.io.sqlite_scope import _cleanup_preserving_error

        mark_inflight_attempts_stopping(now=time.time())
        self._shutdown_control.stop()
        executors = (
            getattr(self, "_executor", None),
            getattr(self, "_curator_executor", None),
        )
        self._executor = None
        self._curator_executor = None
        body_error = None
        try:
            for executor in executors:
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)
            self._shutdown_control.wait()
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            _cleanup_preserving_error(lambda: self._retain_owner_planner_scopes(set()), body_error)

    # LLM: 异常由 typed 退避组件隔离，不能终止 supervisor、消费事件或修改 Goal；慢流中的执行不经过此门。
    # LLM: 仅本宿主关闭期间的typed中断不计业务失败；其它异常仍沿原退避与结构化诊断。
    # 函数用途: 安全运行一个工作片；成功清除旧冷却，失败记录原诊断并等待对应恢复条件。
    def _safe_thread_tick(
        self,
        scheduler: BackgroundMainAgentScheduler,
        label: str,
        thread_id: str,
    ) -> list[object]:
        try:
            result = list(scheduler.tick_thread(thread_id) or [])
            self._lane_retry.succeeded(label, thread_id)
            return result
        except Exception as exc:
            if isinstance(exc, InterruptedError) and self._shutdown_control.stopping:
                return []
            self._record_thread_failure(label, thread_id, exc)
            return []




# LLM: supervisor 复用 owner agent 与规范会话存储；独立 thread 在有界池并发，模型/权限由工作片作用域隔离。
# 类用途: 统一后台会话、owner 发现和低优先级维护编排；不重复创建 Gateway 或修改持久任务的事实源。
class _BackgroundMainSupervisor(_BackgroundThreadLaneSupervisorMixin):
    """单 Gateway 的 owner/thread 后台调度入口，渠道交付沿原 DeliveryService。"""

    # LLM: 复用组合根和 owner 池；进程内执行池/退避不替代 durable claim，初始化不发模型请求。
    # LLM: 原context的停止观察与此supervisor一次绑定，不能用新对象替换在途责任。
    # 函数用途: 接线后台会话与维护资源，并建立可随进程退出回收的调度状态。
    def __init__(self, context: GatewayRunContext) -> None:
        self._startup_started_at = time.monotonic()
        self._base_agent = _gateway_agent_from_context(context)
        self._shutdown_control = getattr(context, "background_shutdown", None) or BackgroundShutdown()
        self._shutdown_control.bind(self)
        self._channels = self._base_agent.delivery_service
        self._base_scheduler = _build_background_scheduler(self._base_agent, self._channels)
        self.poll_interval = _background_main_poll_interval()
        self._registry = shared_active_owner_registry(context.agent)
        self._owner_pool: object | None = None
        self._owner_schedulers: dict[int, BackgroundMainAgentScheduler] = {}
        self._owner_planner_sqlite_scopes: dict[object, object] = {}
        self._owner_planner_scope_used_at: dict[object, float] = {}
        self._owner_idle_streaks: dict[object, int] = {}
        self._owner_next_check_at: dict[object, float] = {}
        self._owner_plan_signatures: dict[object, tuple | None] = {}
        self._next_owner_retire_at = 0.0
        # 后台整合按 owner + thread 分车道。持久 run claim 保证同会话单飞，
        # 进程内 in-flight 去重防止同一 thread 重复提交；不同 thread 可在有界池中并发。
        self._executor: object | None = None
        # 记忆策展走独立低优先级车道；不能占满主会话/child lifecycle wake 的工位。
        self._curator_executor: object | None = None
        # 会话运行时 的 active turn 是 thread-scoped；这里也以 durable thread_id 作最小运行车道。
        self._inflight: dict[object, object] = {}
        self._lane_retry = BackgroundLaneRetry()
        # 磁盘级唤醒发现(治「睡死叫不醒」§1):登记表是进程内易失结构,网关重启清零、
        # LRU 会逐出,且只有新入站请求才补记;长盯守非阻塞挂起期恰恰没有新请求 →
        # scoped owner 的到点 policy 从此无人消费。这里启动即扫一次、之后按间隔重扫,
        # 把磁盘上「有 enabled policy / 待处理唤醒信号」的 owner 种回登记表,重启自愈。
        self._wake_rescan_interval = BACKGROUND_OWNER_WAKE_RESCAN_SECONDS
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

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 读取当天Curator已用配额，跨天重置而非恢复旧日额度。
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

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 按节流周期写调度器存活心跳，供只读诊断观察。
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

    # LLM: 先回收完成车道，再限频释放无持久工作的空闲 owner；实例回收不能改变任务/唤醒状态。
    #   安全重启排空期间只回收已完成车道，不发现/提交新的会话或策展工作片，持久唤醒留给接班进程。
    # 函数用途: 推进后台调度、按需释放空闲用户缓存，并记录完成报告。
    def tick(self) -> bool:
        from ..agent.gateway_parts.owner_retention import release_idle_owner_agents

        self._maybe_write_heartbeat()
        draining = restart_draining()
        if not draining:
            self._maybe_seed_wake_pending_owners()
        reports = self._collect_finished_ticks()
        if not draining:
            now = time.monotonic()
            if now >= self._next_owner_retire_at:
                release_idle_owner_agents(self)
                self._next_owner_retire_at = now + 5.0
            self._sync_owner_schedulers()
            self._recover_active_watch_harvesters()
            self._run_due_curators()
            self._submit_ready_thread_ticks()
        if not reports:
            return False
        _record_background_main_reports(self._base_agent, reports)
        return True

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 收割已完成的维护future，保留异常和后续调度边界。
    def _collect_finished_ticks(self) -> list[object]:
        out: list[object] = []
        for key in list(self._inflight):
            future = self._inflight[key]
            if not getattr(future, "done", lambda: True)():
                continue
            self._inflight.pop(key, None)
            out.extend(self._tick_result(future, key))
        return out

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 解析一次维护future的真实结果，不把异常写成成功。
    def _tick_result(self, future: object, key: object) -> list[object]:
        try:
            return list(future.result() or [])
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.owner_result", str(key), exc)
            return []

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 按有界条件把待唤醒owner加入登记表，避免每拍全量播种。
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

    # LLM: 只为登记表硬事实保留会话调度器，仅需记忆整理的所有者延后到有界记忆车道内实例化。
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
                pool.get(owner, hard=True, touch=False)
            except Exception as exc:
                _print_gateway_loop_error("gateway_background_main.owner_build", str(getattr(owner, "owner_id", "")), exc)
        active = {id(agent): agent for agent in pool.hard_agents()}
        for key in list(self._owner_schedulers):
            if key not in active and not self._active_threads_for_owner(key):
                self._owner_schedulers.pop(key, None)  # scoped agent 被 LRU 逐出且没在跑 → 丢弃其调度器
                self._drop_owner_planner_scope(key)
        for key, scoped_agent in active.items():
            if key not in self._owner_schedulers:
                self._owner_schedulers[key] = _build_background_scheduler(scoped_agent, self._channels)

    # LLM: 盯守恢复只检查硬事实所有者代理，因为持久活动盯守本身就是登记表硬事实，仅有记忆整理的所有者不可能拥有它。
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

    # LLM: 完成回收不受新任务节流影响；记忆策展与自学习都关闭时不得创建软 owner，运行中的 future 仍按原合同结束。
    #   自学习开启时即使 memory_curator_enabled=false 也要运转本车道，自动总结 Skill 请求才会被处理。
    # 函数用途: 及时释放已完成的记忆整理实例，再按开关、有限 worker 和轮转游标调度到期工作。
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
        soft_agent_ids = getattr(self, "_curator_soft_agent_ids", set())
        self._curator_soft_agent_ids = soft_agent_ids
        pool = getattr(self, "_owner_pool", None)
        _retire_finished_curators(self, soft_agent_ids, pool)
        now = time.monotonic()
        if not _background_curation_enabled(self._base_agent) or now < self._next_curator_run_at:
            return
        self._next_curator_run_at = now + _positive_int_config(
            self._base_agent, "owner_maintenance_scan_interval_seconds", default=60
        )
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

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 读取当前Curator每日配额配置，不在查询时扣额度。
    def _curator_daily_quota(self) -> int:
        return _positive_int_config(self._base_agent, "curator_daily_quota", default=_CURATOR_DAILY_QUOTA_COUNT)

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 只读检查Curator当日剩余额度，实际消费另行记账。
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

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 仅在真实Curator事务执行后更新每日消费记录。
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

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 只读Curator待处理原因或活动lease，决定是否进入紧急车道。
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

    # LLM: 记忆整理仍按原准入条件（记忆总闸、紧急原因或当日配额）决定是否运行；自动总结 Skill 在其后独立运行，
    #   两者失败互不影响。准入可能只因为有学习请求，所以这里必须重算记忆整理条件，不能直接跑 run_if_due。
    # 函数用途: 在策展车道里依次执行到期的记忆整理和待处理的自动总结 Skill。
    def _safe_run_curator(self, agent: object, label: str) -> object:
        result = self._run_memory_curator(agent, label) if _curator_run_allowed(self, agent) else None
        _run_skill_learning(agent, label)
        return result

    # LLM: 只有真正执行了一次 curator 事务才扣当日配额；异常只写诊断。
    # 函数用途: 运行一次记忆整理并按结果记账。
    def _run_memory_curator(self, agent: object, label: str) -> object:
        try:
            result = agent.memory_curator.run_if_due()
        except Exception as exc:
            _print_gateway_loop_error("gateway_memory_curator.iteration", label, exc)
            return None
        status = str(getattr(result, "status", "") or "").strip().lower()
        if status in _CURATOR_CONSUMED_STATUSES:
            self._curator_quota_record_consumed()
        return result

    # LLM: 此执行器是独立低优先级容量车道，排队的会话唤醒绝不能排在记忆提取调用后等待。
    # 函数用途: 惰性创建记忆策展专用线程池，并按配置限制全 Gateway 并发。
    def _get_curator_executor(self) -> object:
        if self._curator_executor is None:
            self._curator_executor = DurableDaemonThreadPoolExecutor(
                max_workers=_memory_curator_workers(self._base_agent),
                thread_name_prefix="memory-curator",
            )
        return self._curator_executor

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 惰性取得请求链共用的owner池，失败记录原诊断而不另造池。
    def _ensure_owner_pool(self) -> object | None:
        if self._owner_pool is not None:
            return self._owner_pool
        try:
            from ..agent.gateway_parts.request_worker import _owner_pool

            self._owner_pool = _owner_pool(self._base_agent)  # 与请求路共用 owner 池，配置沿原路径去固化。
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.owner_pool", "background-main", exc)
            self._owner_pool = None
        return self._owner_pool


# LLM: 先回收已结束记忆整理future，再计算容量；软所有者代理只在对应future结束后逐出，硬事实及基础代理保持常驻。
# 逐出失败仅作诊断，不改变调度权威。
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


# LLM: 软代理逐出只是内存清理，不是权限或调度门；逐出失败不得阻塞其他所有者的记忆整理车道。
# 函数用途: 尝试释放一个软 owner agent；失败只写诊断，不打断后台循环。
def _evict_soft_curator_agent(pool: object | None, key: object) -> None:
    if pool is None:
        return
    try:
        pool.evict_soft_agent_id(key)
    except Exception as exc:
        _print_gateway_loop_error("gateway_memory_curator.soft_evict", str(key), exc)


# LLM: 公平性输入按基础代理、常驻硬事实所有者、有界登记表快照排序；公平轮转由游标负责，而不是此收集器。
# 此函数不得按到期状态或额度筛选候选。
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


# LLM: 复用原 pool.peek/get；unknown 不缓存为无活，失败隔离到准确 owner，不造第二逐出器。
# 函数用途: 先查磁盘再物化冷 owner，启动保护期仅允许紧急策展冷构建。
def _resolve_curator_candidate(
    kind: str,
    candidate: object,
    pool: object | None,
    *, supervisor=None,
) -> tuple[object | None, bool]:
    if kind != "owner":
        return candidate, False
    if pool is None:
        return None, True
    try:
        peek = getattr(pool, "peek", None)
        loaded = peek(candidate) if callable(peek) else None
        if loaded is not None:
            return loaded, True
        if supervisor is not None:
            state, urgent = _cold_curator_precheck(supervisor, candidate)
            if state == "not_due" or (_startup_grace_active(supervisor) and not urgent):
                return None, True
            builds = getattr(supervisor, "_curator_cold_builds", 0)
            if builds >= _COLD_CURATOR_BUILDS_PER_TICK_COUNT:
                return None, True
            supervisor._curator_cold_builds = builds + 1
        return pool.get(candidate, hard=False), True
    except Exception as exc:
        _print_gateway_loop_error(
            "gateway_memory_curator.owner_build",
            str(getattr(candidate, "owner_id", "")),
            exc,
        )
        return None, True


# LLM: 准入只读类型化记忆策略、待处理原因、额度、持久技能学习队列和准确在途映射；被拒软候选立即逐出。
# 模型正文或推断的任务重要性不参与裁决。
# 函数用途: 判断一个 agent 本轮是否可提交策展车道（记忆整理或自动总结 Skill），并释放被策略跳过的软 agent。
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
    if _curator_run_allowed(supervisor, agent) or _skill_learning_pending(agent):
        return True
    if is_soft:
        _evict_soft_curator_agent(pool, key)
    return False


# LLM: 原记忆整理准入条件原样保留：全局 memory_curator_enabled、owner 记忆总闸开启，且有紧急原因
#   （pending reason/active lease）或当日配额。车道只因自学习运转时，这里让记忆整理保持不运行。
# 函数用途: 判断这个 agent 现在能否运行记忆整理。
def _curator_run_allowed(supervisor: object, agent: object) -> bool:
    config = getattr(agent, "config", None)
    if not getattr(config, "memory_curator_enabled", True) or not _agent_memory_enabled(agent):
        return False
    return bool(supervisor._curator_has_pending_reason(agent) or supervisor._curator_quota_available())


# LLM: 只读 agent.skill_learning 的持久队列；服务缺席（自学习关闭）或检查异常都按没有请求处理。
# 函数用途: 判断这个 agent 是否有待处理的自动总结 Skill 请求。
def _skill_learning_pending(agent: object) -> bool:
    has_pending = getattr(getattr(agent, "skill_learning", None), "has_pending", None)
    if not callable(has_pending):
        return False
    try:
        return bool(has_pending())
    except Exception:
        return False


# LLM: 自动总结失败只写诊断，不影响同车道的记忆整理结果与其它 owner。
# 函数用途: 处理这个 agent 最早的一条自动总结 Skill 请求。
def _run_skill_learning(agent: object, label: str) -> None:
    run_pending = getattr(getattr(agent, "skill_learning", None), "run_pending", None)
    if not callable(run_pending):
        return
    try:
        run_pending()
    except Exception as exc:
        _print_gateway_loop_error("gateway_skill_learning.iteration", label, exc)


# LLM: 策展车道的全局开关：记忆整理或自学习任一开启即运转；两者都关时不实例化任何软 owner。
# 函数用途: 判断后台策展车道是否需要运转。
def _background_curation_enabled(agent: object) -> bool:
    config = getattr(agent, "config", None)
    return bool(getattr(config, "memory_curator_enabled", True) or getattr(config, "enable_self_learning", False))


# LLM: 只有提交辅助入口可修改记忆整理在途映射；软代理在future结束前保持常驻，提交失败先逐出再上抛，让监督器重试面对干净代理池。
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


# LLM: 每检查一个候选即推进游标；每拍两个冷构建，保护期仅延迟非紧急策展，不阻止 future 核收。
# 函数用途: 公平扫描候选，限制冷实例初始化成本并保存下轮位置。
def _submit_due_curators(
    supervisor: object,
    candidates: list[tuple[str, object]],
    available: int,
    soft_agent_ids: set[object],
    pool: object | None,
) -> int:
    index = int(getattr(supervisor, "_curator_candidate_cursor", 0) or 0) % len(candidates)
    examined = 0
    supervisor._curator_cold_builds = 0
    while available > 0 and examined < len(candidates):
        if supervisor._curator_cold_builds >= _COLD_CURATOR_BUILDS_PER_TICK_COUNT:
            break
        kind, candidate = candidates[index]
        index = (index + 1) % len(candidates)
        examined += 1
        if (kind != "owner" and _startup_grace_active(supervisor)
                and not supervisor._curator_has_pending_reason(candidate) and not _skill_learning_pending(candidate)):
            continue
        agent, is_soft = _resolve_curator_candidate(kind, candidate, pool, supervisor=supervisor)
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


# LLM: 只推迟非紧急retention/闲置压缩；安全清理在保护期照跑，原分页与owner边界保持，不创建冷Agent。
# 类用途: 分页维护磁盘保留和空闲会话压缩，沿原安全清理及已加载执行器范围运行。
class _GatewayOwnerMaintenanceController:
    """Apply one bounded owner retention page per tick without creating agents."""

    # LLM: 只给非紧急 retention 建启动时钟，安全恢复不经本 controller。
    # 函数用途: 绑定基础目录、维护游标和交互保护窗口。
    def __init__(self, context: GatewayRunContext) -> None:
        self._startup_started_at = time.monotonic()
        self._base_agent = _gateway_agent_from_context(context)
        self._base_home = self._base_agent.home_paths
        self._cursor: OwnerWakeCursor | None = None
        self._idle_compact_cursor: dict = {}
        self.interval = max(
            0.0,
            _float_config(
                self._base_agent,
                "owner_maintenance_scan_interval_seconds",
                default=60.0,
            ),
        )

    # LLM: 节奏交给 _run_loop_with_backoff：出错记账、限流打印、等 max(interval, 退避)。
    # 函数用途: 跑维护循环直到 stop_event 置位。
    def run(self, stop_event: threading.Event) -> None:
        _run_loop_with_backoff(
            stop_event, self.tick,
            _LoopSite("owner-maintenance", "gateway_owner_maintenance.tick", "owner-maintenance", self.interval),
        )

    # LLM: 每拍先跑基础 owner，再按分页游标跑一页 scoped owner，只做到期维护、不构造 owner agent；
    #   摘要由 _maintenance_summary 汇总（failed 只算整次被拒与执行期动作失败，另有 refused / isolated），只打印、不落盘。
    # 函数用途: 保护期后跑原retention分页及最多两条闲置会话，不推迟租约及未知结果恢复。
    def tick(self, *, now: float | None = None) -> dict[str, int]:
        _cleanup_loaded_runtime_safety(self._base_agent)
        if _startup_grace_active(self):
            return _maintenance_summary([], 0)
        current = float(now if now is not None else time.time())
        reports = [run_owner_retention_if_due(self._base_home, now=current)]
        from ..agent.gateway_parts.compact_operations import idle_compact_maintenance_tick

        idle_compact_maintenance_tick(self._base_agent, cursor=self._idle_compact_cursor, now=current)
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
        summary = _maintenance_summary(reports, page.scanned)
        if summary["ran"] or summary["failed"]:
            print(
                "[gateway-owner-maintenance] "
                + json.dumps(summary, ensure_ascii=False, sort_keys=True),
                flush=True,
            )
        return summary


# LLM: 摘要只打印、不落盘，没有持久化读取方。failed 只算整次被拒（apply_outcome=refused，即 retention 用
#   _POLICY_LEVEL_ERROR_CODES 判定的整份拒绝）和执行期动作失败；「执行了、只有路径级隔离错误」只进 isolated；
#   持久化的 status 不受影响。从 _GatewayOwnerMaintenanceController.tick 抽出只为控制函数长度，语义不变。
# 函数用途: 把一拍里各 owner 的维护结果汇总成扫描数、跑了几个、失败、被拒、隔离五个计数。
def _maintenance_summary(reports: list[OwnerMaintenanceResult], scanned: int) -> dict[str, int]:
    return {
        "scanned": scanned,
        "ran": sum(report.ran for report in reports),
        "failed": sum(
            report.apply_outcome == "refused" or report.failed_action_count > 0
            for report in reports
        ),
        "refused": sum(report.apply_outcome == "refused" for report in reports),
        "isolated": sum(
            report.apply_outcome == "applied" and report.isolated_error_count > 0
            for report in reports
        ),
    }


# LLM: 原 SQLite due 索引优先；legacy 全盘修复延到启动保护后，不建立第二调度权威。
# 类用途: 立即唤醒已到期 owner，并在空闲阶段补旧索引。
class _GatewaySchedulerDueController:
    """Wake due scoped owners from one indexed projection, then revalidate locally.

    通道运行时 keeps cron jobs in one SQLite store and arms from the earliest
    ``nextRunAt``.  My-agent keeps each owner's JSON ledger as authority, so
    this controller adapts that pattern with a shared SQLite due-owner index.
    It never executes a job or reads another owner's prompt.
    """

    # LLM: 索引唤醒立即运行；只有一次性 legacy 全盘修复受保护期约束。
    # 函数用途: 绑定原调度索引、启动时钟和修复完成标志。
    def __init__(self, context: GatewayRunContext) -> None:
        self._startup_started_at = time.monotonic()
        self._legacy_repair_done = False
        self._base_agent = _gateway_agent_from_context(context)
        self._registry = shared_active_owner_registry(context.agent)
        repository = getattr(self._base_agent, "scheduler_repository", None)
        due_index = getattr(repository, "due_index", None)
        self._due_index = due_index or SchedulerDueIndex(
            self._base_agent.home_paths.global_index_dir / "scheduler_due.sqlite3"
        )
        self.interval = _background_main_poll_interval()
        pool_limit = _positive_int_config(
            self._base_agent,
            "owner_agent_pool_max_agents",
            default=64,
        )
        self._claim_limit = max(1, min(pool_limit, _background_owner_workers(self._base_agent)))
        self._lease_seconds = max(30.0, self.interval * 6)

    # LLM: 未索引旧表修复不得挡在已索引任务的首次 tick 前。
    # 函数用途: 立即运行到期唤醒循环，重型修复交由后续 tick。
    def run(self, stop_event: threading.Event) -> None:
        _run_loop_with_backoff(
            stop_event, self.tick,
            _LoopSite("scheduler-due", "gateway_scheduler_due.tick", "scheduler-due", self.interval),
        )

    # LLM: 失败不标完成，下一拍仍可重试；保护期不阻塞原 due 索引消费。
    # 函数用途: 保护期后补一次旧调度表索引。
    def _repair_legacy_once(self) -> None:
        if getattr(self, "_legacy_repair_done", False) or _startup_grace_active(self):
            return
        try:
            repaired = self._due_index.repair_legacy_owner_ledgers(
                self._base_agent.home_paths.owners_dir
            )
            self._legacy_repair_done = True
            if int(repaired.get("scanned") or 0) or int(repaired.get("errors") or 0):
                print(
                    "[gateway-scheduler-due] legacy repair "
                    + json.dumps(repaired, ensure_ascii=False, sort_keys=True),
                    flush=True,
                )
        except Exception as exc:
            _print_gateway_loop_error("gateway_scheduler_due.repair", "scheduler-due", exc)

    # LLM: 登记表实际提供 snapshot；每次 record 前按同一身份键检查活跃状态，只抑制重复宣告不跳过登记刷新。
    # 函数用途: 先唤醒已索引 owner，再于保护期后补旧索引，不重复刷屏宣告。
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
            active = {(item.provider, item.owner_kind, item.owner_id) for item in self._registry.snapshot()}
            already_active = (owner.provider, owner.owner_kind, owner.owner_id) in active
            self._registry.record(owner)
            if not already_active:
                announced += 1
        if announced:
            print(
                f"[gateway-scheduler-due] due owners announced: {announced}",
                flush=True,
            )
        self._repair_legacy_once()
        return announced


# LLM: 原C组合根标志仅禁止这里直接运行业务，不替代P3/P5或租约；普通Count0无新I/O。
# 函数用途: 判断原controller是否应让既有后台来源进入独立请求主链。
def _coordinator_request_context(context: GatewayRunContext) -> bool:
    return (int(getattr(context, "coordinator_worker_count", 0) or 0) > 0
            or getattr(context, "control_plane", None) is not None)


# LLM: 独立于对话回合的死 runner 孤儿收敛器；复用共享 owner 登记表与独立 scoped-agent 池，巡检节流与完成水位变化联动须与 test_gateway_orphan_reconciler.py 保持一致。
# 类用途: 在网关后台周期性扫描并回收失联的子代理与任务运行态，不阻塞主模型执行。
class _GatewayOrphanReconciler:
    """Reconcile dead runners without waiting for an LLM conversation turn.

    The owner registry is shared with request/background paths, while this
    reconciler has its own scoped-agent pool.  That keeps model execution and
    controller liveness independent; persistence locks remain the authority for
    concurrent state changes.
    """

    # LLM: 正C仅在此关闭直接扫业务，来源发现由原请求生产者负责；Count0保main真实owner池。
    # 函数用途: 记录原组合根结构边界，不创建任何后台执行许可。
    def __init__(self, context: GatewayRunContext) -> None:
        self._coordinator_requests = _coordinator_request_context(context)
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

    # LLM: 孤儿控制器使用有界可运行集合，不得把启动时发现的全部冷所有者预填进执行器私有无界队列。
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
            # 常驻 tick 走统一节奏：出错记账、限流打印、等 max(poll_interval, 退避)。
            _run_loop_with_backoff(
                stop_event, self._async_tick,
                _LoopSite("orphan-reconcile", "gateway_orphan_reconcile.tick", "orphan-reconcile", poll_interval),
            )
        finally:
            self._shutdown_executors()

    # LLM: 正C不直接物化owner或恢复任务，避免绕过独立request/P3/P5；Count0原完整body继续执行。
    # 函数用途: 只在原单进程车道推进监督，正C业务交给原来源运输。
    def _async_tick(self, *, now: float | None = None) -> None:
        if self._coordinator_requests:
            return None
        current = time.monotonic() if now is None else float(now)
        self._collect_finished_sweeps()
        self._submit_base_sweep(current)
        self._submit_owner_sweeps(current)

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 收割已经完成的owner扫描future，不重复提交同一工作。
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

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 消费单次扫描结果，分别保留进展、异常和未知状态。
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

    # LLM: 提交时先占据节拍，执行包装器在完成后延长冷却；single-flight仍由原future维护。
    # 函数用途: 有空位且到期时提交base巡检，不把执行耗时算入下一次休息。
    def _submit_base_sweep(self, now: float) -> None:
        if self._base_inflight is not None or now < self._base_next_at:
            return
        executor = self._base_executor
        if executor is None:
            return
        self._base_next_at = now + self.interval
        self._base_inflight = executor.submit(self._sweep_base)

    # LLM: 成功及异常都在future完成前写入完成时间冷却；max保留已存在的更晚预约。
    # 函数用途: 执行base巡检并保证结束后至少休息一个interval。
    def _sweep_base(self) -> dict[str, object]:
        try:
            return self._sweep(self._base_agent, "base")
        finally:
            self._base_next_at = max(self._base_next_at, time.monotonic() + self.interval)

    # LLM: 提交数量不超过真实工作者容量，并优先选择LRU里最近活动的所有者；排队冷所有者不得让刚重连的长任务等在数十次代理构造之后。
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

    # LLM: 租用准确实例不续空闲期；初始化/租用/巡检失败都在finally记录完成后冷却，不影响其它owner车道。
    # 函数用途: 安全巡检单个用户，退出后至少休息一个interval，避免慢巡检无间隔重跑。
    def _sweep_owner(
        self,
        owner: OwnerIdentity,
        label: str,
    ) -> dict[str, object]:
        key = self._owner_key(owner)
        try:
            pool = self._ensure_owner_pool()
            if pool is None:
                return {"owner": label, "state": "owner_pool_unavailable"}
            scoped = pool.get(owner, touch=False)
            with pool.pin(scoped, touch=False):
                return self._sweep(scoped, label)
        finally:
            self._owner_next_at[key] = max(self._owner_next_at.get(key, 0.0), time.monotonic() + self.interval)

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 从宿主owner事实生成稳定登记键，不用显示名称猜身份。
    @staticmethod
    def _owner_key(owner: OwnerIdentity) -> tuple[str, str, str]:
        return (
            str(getattr(owner, "provider", "") or ""),
            str(getattr(owner, "owner_kind", "") or ""),
            str(getattr(owner, "owner_id", "") or ""),
        )

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 关闭维护执行器并回收其资源，不把未完成工作伪装成完成。
    def _shutdown_executors(self) -> None:
        for name in ("_base_executor", "_owner_executor"):
            executor = getattr(self, name, None)
            setattr(self, name, None)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    # LLM: 同步入口复用后台扫描的租用规则；身份发现和回收各自沿原有结构化账本。
    # 函数用途: 执行一轮同步遗留工作检查，供命令和测试调用。
    # LLM: 正C不直接物化owner或恢复任务，避免绕过独立request/P3/P5；Count0原完整body继续执行。
    # 函数用途: 只在原单进程车道推进监督，正C业务交给原来源运输。
    def tick(self) -> list[dict[str, object]]:
        if self._coordinator_requests:
            return []
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
                scoped = pool.get(owner, touch=False)
                with pool.pin(scoped, touch=False):
                    reports.append(self._sweep(scoped, label))
            except Exception as exc:
                _print_gateway_loop_error("gateway_orphan_reconcile.owner", label, exc)
        return reports

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 将已知owner写入共享登记表，沿原有界播种逻辑。
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

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 为孤儿核对惰性取得原owner池，失败保留原错误诊断。
    def _ensure_owner_pool(self) -> object | None:
        if self._owner_pool is not None:
            return self._owner_pool
        try:
            from ..agent.gateway_parts.request_worker import _owner_pool

            self._owner_pool = _owner_pool(self._base_agent)
        except Exception as exc:
            _print_gateway_loop_error("gateway_orphan_reconcile.owner_pool", "orphan-reconcile", exc)
        return self._owner_pool

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 执行本owner的一次维护扫描，沿原配额、忙态和停止边界。
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
            "route_ownership": str(getattr(report, "route_ownership", "") or ""),
            "response_chars": len(response),
            "task_status": str(getattr(report, "task_status", "") or ""),
        }
        log_gateway_event(agent, "gateway_background_main_reported", payload)
        print(
            "[gateway-background-main] "
            f"reason={payload['reason']} task={payload['task_id']} thread={payload['thread_id']} "
            f"delivery={payload['delivery_status']} commit={payload['commit_kind']} "
            f"route={payload['route_ownership']} wake_handled={payload['wake_handled']} "
            f"chars={payload['response_chars']}",
            flush=True,
        )


# LLM: 每拍控制器分页仍有界，尚未完成的发现周期在相邻页之间不得继承稳态下较长的重扫延迟。
# 函数用途: 决定 owner 磁盘发现下一次运行时间；有后页就下个 tick 继续，整轮扫完才按配置休眠。
def _next_owner_discovery_at(
    now: float,
    interval: float,
    next_cursor: OwnerWakeCursor | None,
) -> float:
    return float(now) if next_cursor is not None else float(now) + max(0.0, float(interval))


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 读取受约束正整数配置，失效输入沿原默认。
def _positive_int_config(agent: SimpleAgent, key: str, *, default: int) -> int:
    try:
        parsed = int(getattr(agent.config, key))
    except (AttributeError, TypeError, ValueError):
        # 缺字段/非数字(测试桩 SimpleNamespace 等)一律回退默认,不崩——后台循环永不停机
        return default
    return parsed if parsed > 0 else default


# LLM: 后台主循环与调度器唤醒控制器的 tick 节奏只由两个代码常量推导（请求轮询与心跳取小，夹在 1～5 秒），不再读配置；
#   两处调用方都用它再推导 lease 秒数，改公式要一起看。
# 函数用途: 算后台主循环每拍等多久（默认算出 1 秒）。
def _background_main_poll_interval() -> float:
    return max(1.0, min(5.0, GATEWAY_REQUEST_POLL_INTERVAL_SECONDS, GATEWAY_HEARTBEAT_INTERVAL_SECONDS))


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 函数用途: 读取维护循环浮点配置，保留原默认与范围处理。
def _float_config(agent: SimpleAgent, key: str, *, default: float) -> float:
    try:
        return float(getattr(agent.config, key))
    except (TypeError, ValueError):
        return default


# LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
# 类用途: 保存过期lease恢复扫描节流状态，避免每拍重复全扫。
class _RecoverThrottle:
    """worker-0 的 stale lease 恢复扫描节流：按 processing 超时的 1/3（至少 2 秒）执行。

    恢复扫描要遍历 processing 目录并逐文件读 JSON，原来每个轮询周期（0.2s）都跑，
    空闲时是主要的无效 IO；节流后检测延迟上界仍远小于 lease 超时。"""

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 初始化过期lease扫描节流时间，不在构造时启动扫描。
    def __init__(self, agent: SimpleAgent) -> None:
        timeout = _float_config(agent, "gateway_processing_timeout_seconds", default=120.0)
        self._interval = max(2.0, timeout / 3.0 if timeout > 0 else 30.0)
        self._next_at = 0.0

    # LLM: Gateway后台维护；沿原owner实例、配额、停机与future生命周期，不建立第二任务账，联测维护拍。
    # 函数用途: 判断当前时刻是否达到恢复扫描间隔，并更新节流状态。
    def due(self) -> bool:
        now = time.monotonic()
        if now < self._next_at:
            return False
        self._next_at = now + self._interval
        return True

    # LLM: 恢复段失败时由派发者调用，只把下次到期往后推（取更晚者），不缩短既有节流；0 表示不推。
    # 函数用途: 恢复扫描失败后按退避推迟下一次扫描。
    def defer(self, seconds: float) -> None:
        if seconds > 0:
            self._next_at = max(self._next_at, time.monotonic() + float(seconds))


# LLM: 心跳文件写入循环：节奏是 lease_service.GATEWAY_HEARTBEAT_INTERVAL_SECONDS（至少 1 秒），写失败只记日志不退出；
#   载荷字段与代际锚点见 _write_gateway_heartbeat。
# 函数用途: Gateway 主进程的心跳线程主体，stop_event 置位后退出。
def _gateway_heartbeat_loop(context: GatewayRunContext, stop_event: threading.Event) -> None:
    paths = context.paths
    agent = context.agent

    backoff = LoopErrorBackoff()
    site = _LoopSite("heartbeat", "gateway_heartbeat.write", "heartbeat")
    while not stop_event.is_set():
        # 心跳写失败(磁盘满/瞬时 IO 错)不能杀心跳线程:线程一死,外部把"心跳停更"当网关死。
        # 心跳不退避：它的节奏本身就是限流（5 秒一次），退避只会把"磁盘腾出后重新被看见"推迟到 30 秒；打印照常限流、计数照常记。
        try:
            _write_gateway_heartbeat(paths, agent, status="running", pid=os.getpid())
        except Exception as exc:
            _note_loop_tick_error(site, exc, backoff)
        else:
            backoff.record_success()
        stop_event.wait(max(1, GATEWAY_HEARTBEAT_INTERVAL_SECONDS))


# LLM: 心跳是本代"HTTP 已 bind 并可服务"的发布信号，所以它必须带代际锚点 started_at（语义与 state 的
#   started_at 相同），否则同 PID 的上一代残留 running 无法被 status_rendering._record_is_current_generation
#   拒绝，判据只能靠 pid+running 误判就绪。锚点在写入函数内部解析（见 _heartbeat_generation_started_at），
#   调用方签名与发布顺序都不变：心跳循环照旧只传 status/pid，终止清理心跳也不新增参数。
#   本函数只写心跳文件，不改state；C允许无业务Agent但必须原lease末门，撤权或替换不能回落裸写。
#   队列计数复用一次，首次卡死在发布stalled前保全现场；诊断失败不得阻止原lease门写心跳。
# 函数用途: 写入一份 Gateway 心跳载荷（含本代 started_at），供外部判活、判就绪与诊断读取。
def _write_gateway_heartbeat(
    paths: GatewayPaths,
    agent: SimpleAgent | None,
    *,
    status: str,
    pid: int,
) -> None:
    from ..agent.common.global_write_guard import require_global_writer
    from ..agent.gateway_parts.multiprocess_coordinator import (
        CoordinatorUnavailable,
        coordinator_for_root,
    )

    require_global_writer("gateway_heartbeat")
    lease = coordinator_for_root(paths.root, context=paths)
    if agent is None and lease is None:
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    counts = gateway_request_counts(paths, include_archives=False)
    health = evaluate_dispatcher_stall(loop_health.snapshot(), pending_count=counts.get("pending", 0))
    if loop_health.note_dispatcher_stall(bool(health["dispatcher_stalled"]) and status == "running"):
        _record_dispatcher_stall(paths, agent, health)
    _commit_gateway_heartbeat(
        paths, lease,
        {
            "status": status,
            "pid": pid,
            "started_at": _heartbeat_generation_started_at(paths, pid),
            "updated_at": time.time(),
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace) if agent is not None else "",
            "task_continuation": "owner_scoped_event_driven",
            "request_counts": counts,
            "queue_ages": gateway_queue_ages(paths),
            # 两层限流在飞快照(全局 total + 每用户计数):压测/排障看"谁占着坑"。
            "inflight": admission.snapshot(),
            # 派发线程存活与最近一次 tick：有 pending 却没人派发时，先看这里而不是只看 pending 数。
            **health,
        },
    )


# LLM: 只采集本进程且禁发信号，避免抢占原SIGUSR1；原健康结果不因磁盘/诊断异常而变成功或停止发布。
# 函数用途: 为首次派发卡死保存私有栈、内核事实及Gateway事件和文本日志。
def _record_dispatcher_stall(paths, agent, health):
    reason = health["dispatcher_stall_reason"]
    try:
        facts = hang_diagnostics.capture_gateway_hang_facts(paths, os.getpid(), stage=reason, send_sigusr1=False)
    except Exception as exc:
        facts = {"capture_error": type(exc).__name__}
    payload = {**health, **facts, "pid": os.getpid(), "reason": reason}
    try:
        with paths.log.open("a", encoding="utf-8") as handle:
            handle.write("[gateway-dispatcher-stalled] " + json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        _print_gateway_loop_error("gateway.dispatcher.stall_log", "heartbeat", exc)
    try:
        log_gateway_event(agent, "gateway_dispatcher_stalled", payload)
    except Exception as exc:
        _print_gateway_loop_error("gateway.dispatcher.stall_event", "heartbeat", exc)



# LLM: payload计算在原末写门外；Count0无C时沿原writer，已进入C后原lease丢失/替换一律拒写，不借TTL/心跳文件授信。
# 函数用途: 在原C生命周期短门内写现有心跳文件，默认单进程保原写入路径。
def _commit_gateway_heartbeat(paths, lease, payload):
    from ..agent.gateway_parts.multiprocess_coordinator import (
        CoordinatorUnavailable,
        coordinator_for_root,
        coordinator_role_present,
    )

    if lease is None:
        if coordinator_role_present(paths.root, context=paths):
            raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
        write_json_file(paths.heartbeat, payload)
        return
    with lease._lifecycle_gate:
        if (getattr(paths, "_coordinator_role_lease", None) is not lease
                or lease.control_plane is None or lease.control_plane.paths is not paths
                or coordinator_for_root(paths.root, context=paths) is not lease):
            raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
        write_json_file(paths.heartbeat, payload)


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


# LLM: 所有后台循环的错误打印入口，本函数绝不抛异常：stderr 所在磁盘写满时 print 抛 OSError，让它逃出 except 块就会
#   杀掉调用它的循环线程（2026-09-28 生产事故：派发线程静默退出）。打印失败只在 loop_health 记一笔（不做 IO），
#   经 /status 与心跳可见；磁盘恢复后打印照常。
# 函数用途: 把循环里的异常按结构化报告打印到 stderr，打印失败也不影响调用方。
def _print_gateway_loop_error(context: str, worker_id: str, exc: BaseException) -> None:
    try:
        report = runtime_error_report(exc, context=context)
        report["worker_id"] = worker_id
        print("[gateway-loop-error] " + json.dumps(report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    except Exception as print_exc:
        loop_health.note_print_failure(context, print_exc)
