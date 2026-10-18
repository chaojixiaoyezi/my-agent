# LLM: 模式必须先由内部入口冻结；配置默认路径直接用common纯实现，home仍复用唯一解析及CLI05快照；原P5链与退出未知不变。
# 模块用途: 维持worker心跳和请求消费，停止接单后保留未结清执行责任。
from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
import time

from .io import read_json_file, write_json_file_atomic
from .multiprocess_mode import WorkerLaunch, current_worker_partition
from .multiprocess_state import (
    WORKER_HEARTBEAT_SECONDS,
    prepare_worker_readiness,
    worker_control_action,
    worker_heartbeat,
)

# macOS 每秒确认直接父进程；同一节拍读控制，心跳仍独立保持 2 秒周期。
WORKER_PARENT_POLL_SECONDS = 1.0
# Linux prctl 标准 ABI：1=PR_SET_PDEATHSIG，指定直接父进程退出时给本进程 SIGTERM。
_WORKER_PARENT_DEATH_PRCTL_CODE = 1


# LLM: 原P4/逐消息native握手先于owner池；轮询先保存lane再start，ACK异常仍持原线程收尾；退出先收泵再关闭原客户端，未知不冒充排空。
# 函数用途: 运行worker消费循环，保留启动异常中的原轮询线程，关闭本地槽位并恢复信号处理。
def run_worker(launch: WorkerLaunch, on_heartbeat=None, *, config_path=None) -> int:
    stop = threading.Event()
    previous = _install_stop_handlers(stop)
    pump = None
    control_lane = None
    native_client = None
    try:
        reason = _arm_parent_exit(launch.parent_pid)
        readiness = prepare_worker_readiness(launch) if not reason else {"ready": False, "error_code": "WORKER_NOT_READY", "reason": reason}
        if current_worker_partition() != launch.partition:
            readiness = {"ready": False, "error_code": "WORKER_MODE_REQUIRED"}
        if not readiness["ready"]:
            _publish_heartbeat(launch, readiness, on_heartbeat)
            return 2
        if launch.instance_id:
            wait_worker_registration(launch, readiness, stop)
            from .multiprocess_native_runtime import connect_worker_native

            native_client = connect_worker_native(launch)
        pump = (create_request_pump(launch, config_path) if native_client is None
                else create_request_pump(launch, config_path, native_client=native_client))
        _publish_heartbeat(launch, {**readiness, "inflight": pump.inflight}, on_heartbeat)
        from .multiprocess_control_poller import ControlPollingLane

        controls = getattr(pump, 'controls', None)
        if controls is not None:
            control_lane = ControlPollingLane(controls, stop)
            control_lane.start()
        outcome = _worker_execution_loop(launch, stop, on_heartbeat, pump)
        pump.stop_claiming()
        if control_lane is not None:
            control_lane.close()
        _publish_heartbeat(launch, {"ready": False, "reason": outcome, "loop_health": "stopping",
                                   "inflight": pump.inflight}, on_heartbeat)
        pump.close()
        _publish_heartbeat(launch, {"ready": False, "reason": outcome, "loop_health": outcome,
                                   "inflight": pump.inflight}, on_heartbeat)
        return 0
    except Exception as error:
        _publish_heartbeat(launch, {"ready": False, "reason": "worker_runtime_failed",
                                   "error_code": getattr(error, "error_code", type(error).__name__),
                                   "inflight": pump.inflight if pump is not None else 0}, on_heartbeat)
        raise
    finally:
        try:
            try:
                if pump is not None:
                    pump.stop_claiming()
                if control_lane is not None:
                    control_lane.close()
            finally:
                if pump is not None:
                    pump.close()
        finally:
            try:
                if native_client is not None:
                    native_client.close()
            finally:
                _restore_stop_handlers(previous)


# LLM: 首次启动允许C在spawn后发布登记；只有原P4校验成功才物化owner，超时或停止失败关闭。
# 函数用途: 等待本实例进入当前协调者的原始登记表。
def wait_worker_registration(launch, readiness, stop) -> None:
    from .multiprocess_bootstrap import WorkerBootstrapError
    from .multiprocess_bootstrap_publish import read_worker_registry

    deadline = time.monotonic() + 15.0
    while not stop.is_set() and os.getppid() == launch.parent_pid:
        registry = read_worker_registry(launch.gateway_root)
        if registry is not None and registry["c_run_id"] == launch.partition.c_run_id:
            if any(row["worker"] == launch.partition.index for row in registry["workers"]):
                require_worker_registration(launch, readiness, registry=registry)
                return
        if time.monotonic() >= deadline:
            break
        stop.wait(0.05)
    raise WorkerBootstrapError("worker_registration_unavailable", "registry")


# LLM: 心跳和接单前复核原登记及精确出生身份；不以本进程记忆或旧心跳替代当前登记。
# 函数用途: 撤销或替换登记后阻止旧worker继续领取任务。
def require_worker_registration(launch, readiness, *, registry=None) -> None:
    from .multiprocess_bootstrap_publish import read_worker_registry
    from .multiprocess_bootstrap_ready import registered_worker_heartbeat, require_current_worker

    if registry is None:
        registry = read_worker_registry(launch.gateway_root)
    now = time.time()
    heartbeat = registered_worker_heartbeat(launch, launch.instance_id, readiness, now)
    require_current_worker(registry, heartbeat, launch.partition, now)


# LLM: 原就绪门后先拒缺C工作根，再读一次配置；默认路径共用common低层实现，显式config_path优先；home仍核冻结C根，完整roots只取launch。
# 函数用途: 在原P4根约束内固定本次配置，再把真实owner池和P5队列泵组合起来。
def create_request_pump(launch, config_path, *, native_client=None):
    from ..common.config_paths import default_config_path
    from ..settings import load_config
    from ..settings.services.runtime_config_env import apply_runtime_config_environment
    from ..user_space.home_layout import home_paths
    from ..user_space.home_root import ConfiguredHomeSnapshot, configured_home_root
    from .multiprocess_bootstrap import WorkerBootstrapError, freeze_worker_bootstrap
    from .multiprocess_worker_agent import create_worker_pool
    from .multiprocess_worker_dispatch import WorkerRequestPump

    frozen = freeze_worker_bootstrap(launch)
    if not launch.workspace_roots:
        raise WorkerBootstrapError("worker_workspace_required", "workspace_roots")
    config = apply_runtime_config_environment(load_config(config_path or default_config_path()))
    try:
        selected = configured_home_root(config, env={"MY_AGENT_HOME": str(launch.owner_root)})
    except ValueError as error:
        raise WorkerBootstrapError("constructor_home_mismatch", "home_root") from error
    if selected is None or home_paths(selected).root != launch.owner_root:
        raise WorkerBootstrapError("constructor_home_mismatch", "home_root")
    config.my_agent_home = str(launch.owner_root)
    config.host_home_snapshot = ConfiguredHomeSnapshot(launch.owner_root, config.my_agent_home)
    try:
        configured_home_root(config)
    except ValueError as error:
        raise WorkerBootstrapError("constructor_home_mismatch", "home_root") from error
    limit = max(1, int(config.gateway_global_inflight_limit))
    capacity = (limit + launch.partition.count - 1) // launch.partition.count
    pool = create_worker_pool(frozen, config, launch.workspace_roots[0])
    pool.native_client = native_client
    return WorkerRequestPump(frozen, pool, capacity)


# LLM: drain单向关认领→真实pool close/join→原六类源观察，空P1可本W确认；非空树须原C/kernel证明保持未知，exit原实际finish不伪造drained。
# 函数用途: 收取原队列并在准确当前代控制下消费实际排空观察。
def _worker_execution_loop(launch: WorkerLaunch, stop: threading.Event, notice, pump) -> str:
    next_heartbeat = time.monotonic()
    while not stop.wait(WORKER_PARENT_POLL_SECONDS):
        if os.getppid() != launch.parent_pid:
            return "parent_exited"
        action = worker_control_action(read_json_file(launch.gateway_root / "workers" / "control.json"),
                                       launch.partition.c_run_id)
        if action:
            pump.stop_claiming()
            if action == "exit":
                return "stopped"
        next_heartbeat, ready = _refresh_due_heartbeat(launch, next_heartbeat, notice, pump=pump)
        if not ready:
            return "not_ready"
        pump.poll()
        if action == 'drain' and pump.inflight == 0:
            from .multiprocess_worker_drain import worker_pump_drain_assessment

            if worker_pump_drain_assessment(pump).drained:
                return 'drained'
    return "stopped"


# LLM: 每次发布重新核当前C；关认领后停止盖章，但继续报告原在途数；排空证明由P6另行给出。
# 函数用途: 固定节拍更新本worker健康及排空进度，不把在途清零。
def _refresh_due_heartbeat(launch: WorkerLaunch, next_heartbeat: float, notice, *, pump=None) -> tuple[float, bool]:
    if time.monotonic() < next_heartbeat:
        return next_heartbeat, True
    readiness = prepare_worker_readiness(launch)
    ready = readiness["ready"]
    if ready and launch.instance_id:
        require_worker_registration(launch, readiness)
    if pump is not None:
        readiness["inflight"] = pump.inflight
        if pump.claiming_stopped:
            readiness.update(ready=False, loop_health="draining")
    _publish_heartbeat(launch, readiness, notice)
    return time.monotonic() + WORKER_HEARTBEAT_SECONDS, ready


# LLM: 唯一心跳路径由 C 固定的队列根与分区决定；原子替换写完后才发通知，通知不是第二事实源。
# 函数用途: 持久化一份完整心跳及结构化失败/结清原因。
def _publish_heartbeat(launch: WorkerLaunch, readiness: dict, notice) -> None:
    if launch.instance_id:
        from .multiprocess_bootstrap_ready import registered_worker_heartbeat

        payload = registered_worker_heartbeat(launch, launch.instance_id, readiness, time.time())
    else:
        payload = worker_heartbeat(launch, readiness, time.time())
    if "loop_health" in readiness:
        payload["loop_health"] = readiness["loop_health"]
    write_json_file_atomic(launch.gateway_root / "workers" / f"w{launch.partition.index}.json", payload)
    if notice is not None:
        notice(payload)


# LLM: PDEATHSIG 只用于自退，不足以证明整个执行树已死。失败保守拒绝就绪，不降级为无父进程保护。
# 函数用途: Linux 绑定父死亡信号，所有平台都再核对直接父 PID。
def _arm_parent_exit(parent_pid: int) -> str:
    if sys.platform == "linux":
        library = ctypes.CDLL(None, use_errno=True)
        if library.prctl(_WORKER_PARENT_DEATH_PRCTL_CODE, signal.SIGTERM, 0, 0, 0) != 0:
            return "parent_death_signal_unavailable"
    return "parent_exited" if os.getppid() != parent_pid else ""


# LLM: 只对本测试/worker 进程安装 SIGTERM/SIGINT，处理器不写盘，事件把控制交还主循环。
# 函数用途: 让信号可中断等待并触发正常骨架收尾。
def _install_stop_handlers(stop: threading.Event) -> dict:
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, lambda *_args: stop.set())
    return previous


# LLM: 退出路径恢复调用进程原处理器；不操作别的进程。
# 函数用途: 撤回本骨架安装的信号处理。
def _restore_stop_handlers(previous: dict) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)
