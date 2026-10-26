# LLM: 监护沿原配置/home及Gateway身份读门管理进程；Count0强停支持POSIX/Windows，不扩大代际权限或把监护者存活当业务ready。
# 模块用途: 消费CLI显式装配的同Agent/路径，管理原Gateway健康与重启；不反向依赖CLI或改变责任/安全接班。
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from .daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    get_running_pid,
    get_running_pid_report,
    read_pid_file,
    read_pid_record_report,
    read_runtime_status,
    remove_pid_file_if_owned,
    write_targeted_gateway_stop_request,
)
from .daemon_metadata import _flocked_sidecar, build_process_identity, process_identity_is_live
from .io import read_json_file_report, write_json_file_atomic
from .paths import GatewayPaths, gateway_paths
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit
from .restart_service import restart_completed_path, restart_request_path, restart_state_path
from .status_rendering import _iso_timestamp, gateway_readiness

# 计划重启等待旧实例交接的宽限秒数。
PLANNED_RESTART_HANDOVER_GRACE_SECONDS = 180.0
# 计划重启等待新实例启动就绪的宽限秒数。
PLANNED_RESTART_STARTING_GRACE_SECONDS = 120.0
# 慢速重启发生后的退避秒数。
SLOW_RESTART_BACKOFF_SECONDS = 300.0


# LLM: 只比较结构化时间；坏值、非有限值及未来时间不能延长交接宽限，不读日志或mtime。
# 函数用途: 检查一个交接时间是否落在给定的最近窗口内。
def _recent_handover_timestamp(value: object, now: float, grace: float) -> bool:
    try:
        timestamp = float(value or 0.0)
        return math.isfinite(timestamp) and 0.0 <= now - timestamp <= grace
    except (TypeError, ValueError, OverflowError):
        return False


# LLM: 原四个事实源不变；排空phase须180秒内新鲜记录或原Count0身份实证存活，不以残留阶段永久暂停监护。
# 函数用途: 只在仍可信的排空、原子交接或接班启动窗口避让，不授ready/死亡/迁移。
def _planned_restart_handover_active(supervisor: GatewaySupervisor, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    paths = supervisor._paths
    for path, field, pending in ((restart_request_path(paths), "requested_at", True),
                                 (restart_completed_path(paths), "drained_at", False)):
        report = read_json_file_report(path, context="gateway.supervisor.restart_handover.read")
        payload = report.payload
        if (report.load_error is None and isinstance(payload, dict)
                and (not pending or payload.get("status") == "pending")
                and _recent_handover_timestamp(payload.get(field), now, PLANNED_RESTART_HANDOVER_GRACE_SECONDS)):
            return True
    report = read_json_file_report(paths.state, context="gateway.supervisor.handover_state.read")
    state = report.payload if report.load_error is None and isinstance(report.payload, dict) else {}
    phase = state.get("restart_drain")
    if isinstance(phase, dict) and phase.get("phase") in {"turn_wait", "tool_drain", "exiting"}:
        if _restart_drain_handover_active(supervisor, state, phase, now):
            return True
    report = read_json_file_report(restart_state_path(paths), context="gateway.supervisor.restart_state.read")
    restart_state = report.payload if report.load_error is None and isinstance(report.payload, dict) else {}
    drained_at = restart_state.get("last_drained_at")
    if state.get("termination_kind") == "planned_restart" and _recent_handover_timestamp(
            state.get("stopped_at") or drained_at or state.get("started_at"), now, PLANNED_RESTART_HANDOVER_GRACE_SECONDS):
        return True
    return (state.get("status") == "starting"
            and _recent_handover_timestamp(state.get("started_at"), now, PLANNED_RESTART_STARTING_GRACE_SECONDS)
            and _recent_handover_timestamp(drained_at, now, PLANNED_RESTART_HANDOVER_GRACE_SECONDS))


# LLM: 时间优先取原drain/state字段，缺字段才用原state mtime；ISO沿共享解析，过期/未知仅原Count0身份True可继续避让。
# 函数用途: 给磁盘残留排空阶段加时限，不让崩溃时的phase永久阻止自动恢复。
def _restart_drain_handover_active(supervisor, state: dict, drain: dict, now: float) -> bool:
    stamp = drain.get("updated_at") or drain.get("started_at") or state.get("updated_at")
    if not stamp:
        try:
            stamp = supervisor._paths.state.stat().st_mtime
        except OSError:
            stamp = None
    if isinstance(stamp, str):
        try:
            stamp = float(stamp)
        except ValueError:
            stamp = _iso_timestamp(stamp)
    return (_recent_handover_timestamp(stamp, now, PLANNED_RESTART_HANDOVER_GRACE_SECONDS)
            or _standalone_state_identity_status(supervisor)[0] is True)


# LLM: 原Count0无C标记且state.pid与完整出生同绑才观察；坏/缺state非stopped保留None，仅死亡实证或stopped退化为False。
# 函数用途: PID先移除后仍分清存活、死亡及身份未知，缺字段或布尔数值不能取得信号资格。
def _standalone_state_identity_status(supervisor: GatewaySupervisor) -> tuple[bool | None, int | None, dict[str, object] | None]:
    report = read_json_file_report(supervisor._paths.state, context="gateway.supervisor.residual_identity.read")
    state = report.payload
    if report.load_error is not None or not isinstance(state, dict):
        return None, None, None
    count = state.get("worker_count", 0)
    if type(count) is int and count == 0 and "c_run_id" not in state:
        pid = state.get("pid")
        identity = state.get("process_identity")
        if (type(pid) is int and pid > 0 and isinstance(identity, dict)
                and type(identity.get("pid")) is int and identity["pid"] == pid
                and identity.get("host_id") and identity.get("start_time") is not None):
            return process_identity_is_live(identity), pid, identity
    return (False if state.get("status") == "stopped" else None), None, None


# LLM: CLI维护意图原子发布；有限TTL最少一秒，不授予停止进程或绕过身份检查的权限。
# 函数用途: 写入带有效期、原因和操作者的维护锁。
def write_maintenance_lock(paths: GatewayPaths, *, reason: str, actor: str = "cli", ttl_seconds: float = 1800.0) -> dict:
    ttl = float(ttl_seconds)
    if not math.isfinite(ttl):
        raise ValueError("维护锁TTL必须是有限秒数")
    now = time.time()
    lock = {"schema": "my-agent.maintenance-lock.v1", "reason": str(reason), "actor": str(actor),
            "created_at": now, "expires_at": now + max(1.0, ttl), "pid": os.getpid()}
    write_json_file_atomic(paths.maintenance_lock, lock)
    return lock


# LLM: 原子替换允许无副作用读取；缺失、坏JSON、非法有效期均不构成活动锁，不清理过期记录。
# 函数用途: 只读取得尚未到期的维护锁，显式now=0也有效。
def read_maintenance_lock(paths: GatewayPaths, *, now: float | None = None) -> dict | None:
    try:
        lock = json.loads(paths.maintenance_lock.read_text(encoding="utf-8"))
        expires = lock.get("expires_at") if isinstance(lock, dict) else None
        if type(expires) not in (int, float) or not math.isfinite(expires):
            return None
    except (OSError, ValueError, OverflowError):
        return None
    return lock if expires > (time.time() if now is None else now) else None


# LLM: 只删除所选Gateway的维护文件；不存在是幂等成功，不吞掉权限/IO错误。
# 函数用途: 解除维护暂停并报告是否确实删除了锁。
def clear_maintenance_lock(paths: GatewayPaths) -> bool:
    try:
        paths.maintenance_lock.unlink()
    except FileNotFoundError:
        return False
    return True


# LLM: payload与读取错误分列，未知不能当作健康或启动授权。
# 类用途: 承载监护读取结果及结构化错误。
@dataclass(frozen=True)
class SupervisorPayloadReport:
    payload: dict | None
    load_error: dict | None = None


# LLM: 兼容字典投影；需要区分损坏的调用方应读report接口。
# 函数用途: 读取适配器状态，不存在或失败返回空字典。
def read_adapter_state(state_path: Path) -> dict:
    return read_adapter_state_report(state_path).payload or {}


# LLM: 复用原JSON读口及错误报告，不将坏记录标作running。
# 函数用途: 获取适配器状态和读取失败信息。
def read_adapter_state_report(state_path: Path) -> SupervisorPayloadReport:
    if not state_path.exists():
        return SupervisorPayloadReport(None)
    report = read_json_file_report(state_path, context="gateway.supervisor.adapter_state.read")
    return SupervisorPayloadReport(report.payload if report.payload else None, report.load_error)


# LLM: 只投影原心跳文件，存在性不代表角色健康。
# 函数用途: 读取Gateway心跳并保留解析错误。
def read_gateway_heartbeat_report(heartbeat_path: Path) -> SupervisorPayloadReport:
    if not heartbeat_path.exists():
        return SupervisorPayloadReport(None)
    report = read_json_file_report(heartbeat_path, context="gateway.supervisor.heartbeat.read")
    return SupervisorPayloadReport(report.payload if report.payload else None, report.load_error)


# LLM: 仅追加本轮诊断并记录日志，不据错误修补身份或清PID。
# 函数用途: 保存健康检查读取失败原因。
def record_health_load_error(
    load_errors: list[dict],
    load_error: dict,
    *,
    log_warn: Callable[[str], None],
) -> None:
    load_errors.append(load_error)
    log_warn(
        "Gateway health load error: "
        f"context={load_error.get('context', '')} "
        f"path={load_error.get('path', '')} "
        f"reason={load_error.get('reason', '')} "
        f"error={load_error.get('message', '')}"
    )

# LLM: 原PID记录有活进程或读取未知时不是空槽；正N旧代还须完整P6关闭，拒绝冷监护绕过；仍只沿原公开配置启动。
# 函数用途: 确认可启动后创建原Gateway子进程，并有界等其真实角色就绪。
def start_gateway(supervisor) -> int | None:
    supervisor._resolve_agent_and_paths()
    if read_maintenance_lock(supervisor._paths) is not None:
        return None
    report = get_running_pid_report(supervisor._paths.pid, cleanup_stale=False)
    if report.pid is not None or report.load_error is not None:
        supervisor._log_error("Gateway已有进程或身份无法确认；请先用原Gateway状态/停止入口核验，不重复启动。")
        return None
    try:
        from .supervisor_generation import require_startable_generation

        require_startable_generation(supervisor)
    except (OSError, ValueError) as error:
        supervisor._log_error(f"Gateway旧代关闭未确认：{type(error).__name__}")
        return None
    cmd = [sys.executable, "-m", "agent_py_agent", "--config", supervisor.config_path, "gateway", "run"]
    creationflags, start_new_session = _gateway_process_flags()
    try:
        process = _spawn_gateway_process(supervisor, cmd, creationflags, start_new_session)
    except OSError as exc:
        supervisor._log_error(f"Gateway子进程启动失败：{type(exc).__name__}")
        return None
    from .supervisor_generation import retain_spawned_gateway

    retain_spawned_gateway(supervisor, process)
    return _wait_for_gateway_start(supervisor, process)


# LLM: 沿平台原脱离终端方式创建子进程，不提升权限。
# 函数用途: 返回Windows创建标志或POSIX新会话开关。
def _gateway_process_flags() -> tuple[int, bool]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return flags, False
    return 0, True


# LLM: home/config来自原可信Agent；低碎片参数只填未设置项，不覆盖服务管理器显式选择。
# 函数用途: 在同一宿主目录记录启动日志并创建原Gateway进程，不复制配置或业务状态。
def _spawn_gateway_process(supervisor, cmd: list[str], creationflags: int, start_new_session: bool):
    supervisor._paths.root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.setdefault("MALLOC_ARENA_MAX", "2")
    environment.setdefault("MALLOC_TRIM_THRESHOLD_", "131072")
    environment["MY_AGENT_HOME"] = str(supervisor._agent.home_paths.root.resolve())
    environment["MY_AGENT_CONFIG"] = str(Path(supervisor.config_path).expanduser().resolve())
    with supervisor._paths.log.open("ab") as log_file:
        return subprocess.Popen(
            cmd, cwd=str(Path(supervisor.config_path).resolve().parent.parent),
            env=environment, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=creationflags, start_new_session=start_new_session,
        )


# LLM: 原120秒预算内静音预期未就绪；超时再记录一次原因，ready仍核原child出生，不以PID充成功。
# 函数用途: 等Gateway自己的身份、running记录和dispatcher心跳同时成立，失败只返回未就绪。
def _wait_for_gateway_start(supervisor, process) -> int | None:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            supervisor._log_error("Gateway子进程在就绪前已退出。")
            return None
        if supervisor._is_gateway_healthy(quiet_unconfirmed=True):
            report = get_running_pid_report(supervisor._paths.pid, cleanup_stale=False)
            if report.pid == process.pid:
                from .supervisor_generation import complete_gateway_start

                return complete_gateway_start(supervisor, process, report.pid)
        time.sleep(0.5)
    supervisor._is_gateway_healthy(quiet_unconfirmed=False)
    supervisor._log_error("Gateway在120秒内未确认就绪；保留进程与记录供原状态入口核验。")
    return None


# LLM: 原角色/出生门保持；PID先移除时仅Count0原state补充三值等待，末核后才TERM/KILL，未知拒止，不授P6关闭。
# 函数用途: 关停准确旧Gateway，包括先清PID但尚未退出的单进程，保留TERM成功短路与原强停门。
def stop_gateway(supervisor, timeout: float = 20.0, *, expected_identity: dict | None = None) -> bool:
    supervisor._resolve_agent_and_paths()
    report = get_running_pid_report(supervisor._paths.pid, cleanup_stale=False)
    if report.load_error is not None:
        return False
    if report.pid is None:
        if expected_identity is not None:
            return process_identity_is_live(expected_identity) is False
        live, residual_pid, residual_identity = _standalone_state_identity_status(supervisor)
        if live is not True:
            return live is False
        if _wait_gateway_exit(supervisor, residual_pid, residual_identity, timeout):
            return True
        if process_identity_is_live(residual_identity) is not True:
            return False
        terminate_pid(residual_pid)
        if _wait_gateway_exit(supervisor, residual_pid, residual_identity, 5):
            return True
        return _force_stop_gateway(supervisor, residual_pid, residual_identity)
    record = read_pid_record_report(supervisor._paths.pid)
    value = record.payload or {}
    identity = {key: value.get(key) for key in ("host_id", "pid", "start_time")}
    if record.load_error or value.get("kind") != "my-agent-gateway" or identity["pid"] != report.pid:
        return False
    if expected_identity is not None and identity != expected_identity:
        return False
    if process_identity_is_live(identity) is not True:
        return False
    if _write_supervisor_stop_request(supervisor, report.pid, identity) is None:
        return False
    if _wait_gateway_exit(supervisor, report.pid, identity, timeout):
        return True
    if process_identity_is_live(identity) is not True:
        supervisor._log_error("原Gateway身份变化或不可核验，拒绝向该PID发送TERM。")
        return False
    if _capture_unresponsive_gateway_diagnostics(supervisor, report.pid, identity) is False:
        return False
    if process_identity_is_live(identity) is not True:
        supervisor._log_error("原Gateway诊断后身份变化或不可核验，拒绝向该PID发送TERM。")
        return False
    terminate_pid(report.pid)
    if _wait_gateway_exit(supervisor, report.pid, identity, 5):
        return True
    return _force_stop_gateway(supervisor, report.pid, identity)


# LLM: 当前process_control/io无强停原语；POSIX用SIGKILL，缺失时用Windows TerminateProcess对应的SIGTERM；调用者仍须核原代及Count0。
# 函数用途: 跨平台强制终止已核验的原Gateway，发送失败不吞作成功。
def kill_pid(pid: int) -> None:
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


# LLM: TERM失败后重核完整旧身份；Count0使用跨平台强停，正N仍走原完整关闭门，物理退出不等于业务排空。
# 函数用途: 仅Count0同代目标升级强停，确认退出后再清旧代文件。
def _force_stop_gateway(supervisor, pid: int, identity: dict) -> bool:
    if process_identity_is_live(identity) is not True:
        supervisor._log_error("原Gateway身份变化或不可核验，拒绝向该PID发送KILL。")
        return False
    try:
        count = int(getattr(getattr(supervisor._agent, "config", None), "gateway_worker_process_count", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if count != 0:
        return False
    supervisor._log_warn(f"原Gateway(pid={pid})在SIGTERM后未退出，已核验同代身份并升级强制终止。")
    try:
        kill_pid(pid)
    except OSError as error:
        supervisor._log_warn(f"Gateway强制终止发送未确认：{type(error).__name__}")
        return False
    exited = bool(_wait_gateway_exit(supervisor, pid, identity, 5))
    if exited:
        _cleanup_force_stopped_gateway(supervisor, identity)
    return exited


# LLM: 使用原写者同一sidecar锁比较后删除；严格比较原身份，不让旧代清理后继或legacy未知请求。
# 函数用途: 强停已确认退出后，只清仍绑定旧Gateway的PID与stop请求。
def _cleanup_force_stopped_gateway(supervisor, identity: dict) -> None:
    paths = supervisor._paths
    try:
        with _flocked_sidecar(paths.pid):
            report = read_pid_record_report(paths.pid)
            record = report.payload or {}
            if (report.load_error is None and record.get("kind") == "my-agent-gateway"
                    and {key: record.get(key) for key in identity} == identity):
                paths.pid.unlink(missing_ok=True)
            with _flocked_sidecar(paths.stop_request):
                stop = read_json_file_report(paths.stop_request, context="gateway.supervisor.stop_cleanup.read")
                if stop.load_error is None and (stop.payload or {}).get("target_process") == identity:
                    paths.stop_request.unlink(missing_ok=True)
    except OSError as error:
        supervisor._log_warn(f"Gateway旧代文件清理未完成：{type(error).__name__}")


# LLM: 只读proc元数据，各文件限量且线程最多16项；不读命令正文，不扩权限，采集失败不伪造诊断。
# 函数用途: 提取卡死Gateway的Linux状态与等待点摘要。
def _linux_gateway_diagnostics(pid: int) -> dict:
    root = Path(f"/proc/{pid}")
    status = _read_gateway_proc_value(root / "status", 8192)
    result = {key: value.strip()[:128] for line in status.splitlines() if ":" in line
              for key, value in [line.split(":", 1)] if key in {"State", "VmRSS", "VmSwap", "Threads"}}
    result["wchan"] = _read_gateway_proc_value(root / "wchan", 128).strip()
    result["task_wchan"] = {}
    deadline = time.monotonic() + 0.5
    try:
        with os.scandir(root / "task") as tasks:
            for entry in islice(tasks, 16):
                if time.monotonic() >= deadline:
                    break
                if entry.name.isdecimal():
                    result["task_wchan"][entry.name] = _read_gateway_proc_value(root / "task" / entry.name / "wchan", 128).strip()
    except OSError as error:
        result["task_error"] = type(error).__name__
    if not any(key in result for key in ("State", "VmRSS", "VmSwap", "Threads")):
        result["status_error"] = status[:128]
    return result


# LLM: proc文件只读且最多limit字符，权限/消失以错误类型呈现；不把失败解释成目标无阻塞。
# 函数用途: 有界读取单个诊断字段。
def _read_gateway_proc_value(path: Path, limit: int) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return stream.read(limit)
    except OSError as error:
        return f"unavailable:{type(error).__name__}"


# LLM: 旧版SIGUSR1默认可致退出，只有同代state已公告注册且末核仍活才发送；诊断不授重启或清记录权。
# 函数用途: 停止请求超时后记录Linux摘要，并请求已注册的Gateway导出全线程栈。
def _capture_unresponsive_gateway_diagnostics(supervisor, pid: int, identity: dict) -> bool | None:
    if sys.platform.startswith("linux"):
        summary = _linux_gateway_diagnostics(pid)
        supervisor._log_warn(f"Gateway(pid={pid})停止超时诊断：{json.dumps(summary, ensure_ascii=False)}")
    if not hasattr(signal, "SIGUSR1"):
        return None
    state = read_json_file_report(supervisor._paths.state, context="gateway.supervisor.diagnostics.read")
    payload = state.payload or {}
    if (state.load_error is not None or payload.get("diagnostic_sigusr1") is not True
            or payload.get("process_identity") != identity or payload.get("pid") != pid):
        supervisor._log_warn("Gateway未公告同代SIGUSR1诊断注册，跳过栈信号。")
        return None
    if process_identity_is_live(identity) is not True:
        supervisor._log_warn("Gateway诊断前身份变化或不可核验，拒绝发送信号。")
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError as error:
        supervisor._log_warn(f"Gateway栈信号发送未确认：{type(error).__name__}")
    return None


# LLM: 只对本S真实保留的原child使用Popen poll/reap；外部C原OS等待不变，绝不由PID重建进程对象。
# 函数用途: 让准确原Gateway退出等待不被已退出但未回收的child僵尸卡住。
def _wait_gateway_exit(supervisor, pid, identity, timeout):
    from .supervisor_generation import wait_spawned_gateway_exit

    result = wait_spawned_gateway_exit(supervisor, pid, identity, timeout)
    return wait_for_pid_exit(pid, timeout) if result is None else result


# LLM: 复用共同targeted stop writer并携带已冻结旧身份；PID锁内末核和原TERM前重核共同阻止换代串杀。
# 函数用途: 让 Supervisor 精确停止刚刚检查到的那一代 Gateway。
def _write_supervisor_stop_request(supervisor, pid: int, expected_identity: dict | None = None) -> dict[str, object] | None:
    return write_targeted_gateway_stop_request(
        supervisor._paths.pid,
        supervisor._paths.stop_request,
        reason="supervisor shutdown",
        target_pid=pid,
        source="gateway_supervisor",
        expected_identity=expected_identity,
    )


# LLM: 计划交接先暂停干预，快预算耗尽改有界慢探针；原C/P4/config和P6双读关闭门不变，未知不启动。
# 函数用途: 避开原安全重启交接，在原精确停止和整代关闭之后执行受节流的恢复。
def restart_gateway(supervisor) -> bool:
    supervisor._resolve_agent_and_paths()
    if read_maintenance_lock(supervisor._paths) is not None:
        return False
    now = time.time()
    if _planned_restart_handover_active(supervisor, now):
        supervisor._log_info("检测到计划内安全重启交接正在进行，跳过Supervisor重启。")
        return False
    if not _restart_budget_allows(supervisor, now):
        return False
    from .supervisor_generation import (
        capture_generation,
        require_closed_observation,
        require_successor,
    )

    supervisor._resolve_agent_and_paths()
    try:
        observed = capture_generation(supervisor)
    except (OSError, ValueError) as error:
        supervisor._log_error(f"旧代恢复身份未确认：{type(error).__name__}")
        return False
    supervisor._restart_count += 1
    supervisor._last_restart_at = time.time()
    supervisor._log_warn(
        f"Gateway unhealthy. Restarting (attempt {supervisor._restart_count}/{supervisor.max_restart_attempts})..."
    )
    expected = None
    if observed is not None:
        from .multiprocess_records import deserialize_record

        original = deserialize_record(observed.registry)['process_identity']
        expected = {key: original[key] for key in ('host_id', 'pid', 'start_time')}
    if not supervisor._stop_gateway(timeout=10.0, **({'expected_identity': expected} if expected is not None else {})):
        supervisor._log_error("原Gateway未确认停止，拒绝启动第二个进程。")
        return False
    try:
        if observed is not None:
            require_closed_observation(supervisor, observed)
    except (OSError, ValueError) as error:
        supervisor._log_error(f"旧代完整关闭未确认，拒绝新启动：{type(error).__name__}")
        return False
    gateway_pid = supervisor._start_gateway()
    if gateway_pid:
        try:
            if observed is not None:
                require_successor(supervisor, observed, gateway_pid)
        except (OSError, ValueError) as error:
            supervisor._log_error(f"新代身份未确认：{type(error).__name__}")
            return False
        supervisor._gateway_pid = gateway_pid
        return True
    supervisor._log_error("Failed to restart gateway")
    return False


# LLM: max<=0/last=0仍拒绝；300秒慢探针先核原cooldown再更新时间，失败的身份捕获也节流；不调用停止/启动。
# 函数用途: 保留快速恢复预算，耗尽后至多每五分钟放行一次原恢复尝试。
def _restart_budget_allows(supervisor, now: float) -> bool:
    exhausted = supervisor._restart_count >= supervisor.max_restart_attempts
    last_at = float(getattr(supervisor, "_last_restart_at", 0.0) or 0.0)
    if exhausted and (supervisor.max_restart_attempts <= 0 or last_at <= 0.0 or not math.isfinite(last_at)
                      or now - last_at < SLOW_RESTART_BACKOFF_SECONDS):
        supervisor._log_error(f"Max restart attempts ({supervisor.max_restart_attempts}) reached. "
                              "Supervisor will continue monitoring but not restart.")
        return False
    if now - last_at < supervisor.restart_cooldown:
        supervisor._log_info(f"Restart cooldown active ({supervisor.restart_cooldown}s). Skipping.")
        return False
    if exhausted:
        supervisor._log_warn(f"Max restart attempts ({supervisor.max_restart_attempts}) previously reached; "
                             f"attempting slow-backoff recovery probe after {int(SLOW_RESTART_BACKOFF_SECONDS)}s...")
        supervisor._last_restart_at = now
    return True


# LLM: 监护只发布自身身份；初始启动也尊重原计划交接，不能在旧进程持锁时创建竞争Gateway。
# 函数用途: 以维护锁和安全重启事实守住冷启动，再运行原监护与停止循环。
def run_supervisor_loop(supervisor) -> int:
    supervisor._log_info("Gateway supervisor starting")
    supervisor._resolve_agent_and_paths()
    supervisor_pid_path = supervisor._paths.supervisor_pid
    _prepare_supervisor_run(supervisor, supervisor_pid_path)
    if read_maintenance_lock(supervisor._paths) is not None:
        supervisor._log_info("Supervisor paused: maintenance lock active")
    elif not _planned_restart_handover_active(supervisor, time.time()) and not supervisor._is_gateway_healthy():
        supervisor._start_gateway()
    _log_supervisor_active(supervisor)
    _monitor_until_stopped(supervisor)
    _finish_supervisor_run(supervisor, supervisor_pid_path)
    return 0


# LLM: PID/state属于supervisor角色；使用原真实process identity，绝不调用写Gateway状态的write_runtime_status。
# 函数用途: 原子登记本监护进程并安装原退出信号处理器，不覆盖Gateway资料。
def _prepare_supervisor_run(supervisor, supervisor_pid_path: Path) -> None:
    identity = build_process_identity()
    write_json_file_atomic(supervisor_pid_path, {**identity, "kind": "my-agent-supervisor", "updated_at": _utc_now_iso()})
    signal.signal(signal.SIGTERM, supervisor._handle_signal)
    signal.signal(signal.SIGINT, supervisor._handle_signal)
    _write_supervisor_state(supervisor, identity, "running")


# LLM: 独立路径只表示监护者生命周期，不为Gateway/worker授ready或owner权；身份始终来自原进程context。
# 函数用途: 原子写监护状态，便于状态命令区分监护进程与业务服务。
def _write_supervisor_state(supervisor, identity: dict, status: str) -> None:
    write_json_file_atomic(supervisor._paths.supervisor_state, {
        "role": "supervisor", "status": status, "pid": identity["pid"],
        "process_identity": identity, "updated_at": _utc_now_iso(),
    })


# LLM: 只展示监护节奏，不将active日志作为Gateway就绪证明。
# 函数用途: 记录当前健康检查与恢复选项。
def _log_supervisor_active(supervisor) -> None:
    supervisor._log_info(
        f"Supervisor active: check_interval={supervisor.check_interval}s, "
        f"heartbeat_timeout={supervisor.heartbeat_timeout}s, "
        f"max_restarts={supervisor.max_restart_attempts}"
    )


# LLM: 健康检查由原间隔驱动；维护判断留在每次检查入口，不缓存锁状态。
# 函数用途: 在收到退出信号前周期检查服务。
def _monitor_until_stopped(supervisor) -> None:
    last_check = time.time()
    consecutive_failures = 0
    while not supervisor._stop_requested:
        time.sleep(1.0)
        now = time.time()
        if now - last_check < supervisor.check_interval:
            continue
        last_check = now
        consecutive_failures = _run_health_check(supervisor, consecutive_failures)


# 网关子进程平滑运行后重置重启计数预算的观察窗口。
RESTART_BUDGET_RESET_SECONDS = 600.0


# LLM: 维护早退及不健康均清连续起点；计划交接优先，原三次失败门/Adapter意图不变，未观察时间不回填为健康。
# 函数用途: 不干扰安全重启或维护，在重新开始的实际健康窗口满600秒后才回收预算。
def _run_health_check(supervisor, consecutive_failures: int) -> int:
    supervisor._resolve_agent_and_paths()
    if read_maintenance_lock(supervisor._paths) is not None:
        supervisor._healthy_since_at = 0.0
        return 0
    healthy = supervisor._is_gateway_healthy()
    now = time.time()
    if not healthy:
        supervisor._last_unhealthy_at = now
        supervisor._healthy_since_at = 0.0
        if _planned_restart_handover_active(supervisor, now):
            supervisor._log_info("检测到Gateway正在执行计划内安全重启交接，跳过本次不健康计数与干预。")
            return 0
    adapter_healthy = supervisor._check_adapter_health()
    if healthy:
        _reset_stable_restart_budget(supervisor, now)
        if not adapter_healthy and read_adapter_state(supervisor._paths.root / "adapter_state.json").get("state") == "running":
            supervisor._restart_adapter()
        return 0
    consecutive_failures += 1
    supervisor._log_warn(
        f"Gateway health check failed (consecutive={consecutive_failures}): "
        "PID file missing, process dead, or heartbeat stale"
    )
    if not adapter_healthy:
        supervisor._log_warn("Adapter is also not running")
    if consecutive_failures >= 3:
        supervisor._restart_gateway()
        return 0
    return consecutive_failures


# LLM: 首个健康采样以now起算，锚点取restart/unhealthy/since最大值；暂停前或更早重启的起点不能越过最新事实。
# 函数用途: 满600秒实际健康观察窗口后才清预算，不把维护间隔或未监测的重启后时间充数。
def _reset_stable_restart_budget(supervisor, now: float) -> None:
    restart_count = int(getattr(supervisor, "_restart_count", 0) or 0)
    last_restart_at = float(getattr(supervisor, "_last_restart_at", 0.0) or 0.0)
    last_unhealthy_at = float(getattr(supervisor, "_last_unhealthy_at", 0.0) or 0.0)
    healthy_since = float(getattr(supervisor, "_healthy_since_at", 0.0) or 0.0)
    if healthy_since <= 0.0:
        healthy_since = now
        supervisor._healthy_since_at = healthy_since
    healthy_anchor = max(last_restart_at, last_unhealthy_at, healthy_since)
    if restart_count > 0 and healthy_anchor > 0.0 and now - healthy_anchor >= RESTART_BUDGET_RESET_SECONDS:
        supervisor._restart_count = 0
        supervisor._last_unhealthy_at = 0.0
        supervisor._healthy_since_at = 0.0
        supervisor._log_info("Gateway已稳定健康运行满600秒，重置监护重启计数。")


# LLM: 仅恢复明确running且PID已死/缺失的Adapter；未知PID读错误拒绝，单调时钟确保失败尝试也至少间隔30秒。
# 函数用途: 用同一配置/home环境拉起原通道，不杀进程、不改用户停止意图。
def restart_adapter(supervisor) -> bool:
    supervisor._resolve_agent_and_paths()
    if read_maintenance_lock(supervisor._paths) is not None:
        return False
    state = read_adapter_state(supervisor._paths.root / "adapter_state.json")
    if state.get("state") != "running":
        return False
    report = get_running_pid_report(supervisor._paths.adapter_pid, cleanup_stale=False)
    if report.pid is not None or report.load_error is not None:
        return False
    now = time.monotonic()
    if now - supervisor._last_adapter_restart_at < 30.0:
        return False
    supervisor._last_adapter_restart_at = now
    cmd = [sys.executable, "-m", "agent_py_agent", "--config", supervisor.config_path,
           "adapter", "start", "--daemon", "--channel", _adapter_restart_channel(state)]
    try:
        _spawn_gateway_process(supervisor, cmd, *_gateway_process_flags())
    except OSError as error:
        supervisor._log_error(f"Adapter子进程启动失败：{type(error).__name__}")
        return False
    return True


# LLM: requested_channel优先，只有单个合法通道才采用列表；不把任意状态值运输成CLI选项。
# 函数用途: 从上次启动记录恢复feishu、qq或all范围。
def _adapter_restart_channel(state: dict) -> str:
    requested = state.get("requested_channel")
    if requested in ("feishu", "qq", "all"):
        return requested
    channels = state.get("channels")
    if isinstance(channels, list) and len(channels) == 1 and channels[0] in ("feishu", "qq"):
        return channels[0]
    return "all"


# LLM: 收尾只移除自己持有的supervisor PID并写独立状态；Gateway stop结果不伪造成功或覆盖Gateway失败事实。
# 函数用途: 停止监护前请求原Gateway退出，保留其自身终态与排空信息。
def _finish_supervisor_run(supervisor, supervisor_pid_path: Path) -> None:
    supervisor._log_info("Supervisor shutting down...")
    stopped = supervisor._stop_gateway(timeout=15.0)
    remove_pid_file_if_owned(supervisor_pid_path)
    _write_supervisor_state(supervisor, build_process_identity(), "stopped" if stopped else "stop_unconfirmed")
    supervisor._log_info("Supervisor stopped")


# LLM: 仅生成状态投影时间，不作为进程身份或租约。
# 函数用途: 返回UTC时区的ISO时间文本。
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_read_adapter_state = read_adapter_state
_read_adapter_state_report = read_adapter_state_report


# LLM: 仅包含监护循环节奏和日志选项；配置/home/工作区只来自CLI已装配的同Agent，不能在此建立第二权威。
# 类用途: 保存监护选项，不解析配置或创建Agent。
@dataclass
class SupervisorConfig:

    heartbeat_timeout: float = 120.0
    check_interval: float = 10.0
    max_restart_attempts: int = 5
    restart_cooldown: float = 30.0
    log_path: Path | None = None


# LLM: CLI只装配一次实际Agent并交同配置路径；本对象消费该引用管理原Gateway，不反向导入CLI或构造另一N/home权威。
# 类用途: 维护原健康检查与有界重启状态，不替代Gateway生命周期身份。
class GatewaySupervisor:

    # LLM: 绑定同一CLI Agent/config；本实例保留重启及连续健康观察时钟，不创建持久第二权威或进程。
    # 函数用途: 初始化原监护参数、进程引用和健康窗口，避免重启预算永久耗尽或过早清零。
    def __init__(
        self,
        config_path: str,
        *,
        agent,
        options: SupervisorConfig | None = None,
    ):
        selected = getattr(getattr(agent, "config", None), "config_path", None)
        if not selected or Path(selected).expanduser().resolve() != Path(config_path).expanduser().resolve():
            raise ValueError("监护需要同一配置解析后的原Agent。")
        self.config_path = str(Path(selected).expanduser().resolve())
        _opts = options or SupervisorConfig()
        self.heartbeat_timeout = _opts.heartbeat_timeout
        self.check_interval = _opts.check_interval
        self.max_restart_attempts = _opts.max_restart_attempts
        self.restart_cooldown = _opts.restart_cooldown
        self.log_path = _opts.log_path

        self._supervisor_pid: int = os.getpid()
        self._gateway_pid: int | None = None
        self._gateway_child = self._gateway_child_origin = None
        self._restart_count: int = 0
        self._last_restart_at: float = 0.0
        self._last_unhealthy_at: float = 0.0
        self._healthy_since_at: float = 0.0
        self._last_adapter_restart_at: float = float("-inf")
        self._stop_requested: bool = False
        self._agent = agent
        self._paths = None
        self._last_health_load_errors: list[dict] = []

    # LLM: 实际Agent只由CLI组合根一次构造并显式传入；这里只投影同对象规范路径，不解析YAML/环境或构造替代Agent。
    # 函数用途: 延迟取得已绑定Agent的GatewayPaths，保留原Count0/正N健康与关闭读门。
    def _resolve_agent_and_paths(self):
        if self._paths is None:
            self._paths = gateway_paths(self._agent)

    # LLM: 统一日志格式并可选追加文件；日志不充当运行状态权威。
    # 函数用途: 输出一条带时间和级别的监护日志。
    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        print(line, flush=True)
        self._append_log_file(line)

    # LLM: 文件日志失败不改变监护生命周期，保留原尽力写策略。
    # 函数用途: 把监护日志追加到可选文件。
    def _append_log_file(self, line: str) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # LLM: 仅转发INFO级别，不建立业务状态。
    # 函数用途: 记录普通监护信息。
    def _log_info(self, msg: str) -> None:
        self._log("INFO", msg)

    # LLM: 仅转发WARN级别，不据日志触发重试。
    # 函数用途: 记录监护告警。
    def _log_warn(self, msg: str) -> None:
        self._log("WARN", msg)

    # LLM: 仅转发ERROR级别，不吞掉调用方失败结果。
    # 函数用途: 记录监护错误。
    def _log_error(self, msg: str) -> None:
        self._log("ERROR", msg)

    # LLM: 兼容payload投影，完整健康判定仍检查report错误。
    # 函数用途: 返回当前Gateway心跳字典。
    def _read_gateway_heartbeat(self) -> dict | None:
        return self._read_gateway_heartbeat_report().payload

    # LLM: 只沿已绑定Agent的规范路径读取，不创建第二配置权威。
    # 函数用途: 读取当前Gateway心跳及错误。
    def _read_gateway_heartbeat_report(self) -> SupervisorPayloadReport:
        self._resolve_agent_and_paths()
        return read_gateway_heartbeat_report(self._paths.heartbeat)

    # LLM: 健康仍核原C和正N全W；启动期仅静音预期unconfirmed，坏记录读错误保留原告警；不清PID。
    # 函数用途: 只读组合原身份和ready事实，拒绝新鲜但属于监护者或旧代的心跳。
    def _is_gateway_healthy(self, *, quiet_unconfirmed: bool = False) -> bool:
        self._resolve_agent_and_paths()
        self._last_health_load_errors = []
        heartbeat_report = self._read_gateway_heartbeat_report()
        if heartbeat_report.load_error is not None:
            self._record_health_load_error(heartbeat_report.load_error)
            return False
        pid_report = get_running_pid_report(self._paths.pid, cleanup_stale=False)
        if pid_report.load_error is not None:
            self._record_health_load_error(pid_report.load_error)
            return False
        record_report = read_pid_record_report(self._paths.pid)
        if record_report.load_error is not None:
            self._record_health_load_error(record_report.load_error)
            return False
        record, pid = record_report.payload or {}, pid_report.pid
        identity = {key: record.get(key) for key in ("host_id", "pid", "start_time")}
        if not pid or record.get("kind") != "my-agent-gateway" or identity["pid"] != pid:
            return self._health_unconfirmed("gateway_role_or_pid_unconfirmed", quiet=quiet_unconfirmed)
        if process_identity_is_live(identity) is not True:
            return self._health_unconfirmed("gateway_birth_unconfirmed", quiet=quiet_unconfirmed)
        state_report = read_json_file_report(self._paths.state, context="gateway.supervisor.state.read")
        if state_report.load_error is not None:
            self._record_health_load_error(state_report.load_error)
            return False
        state, heartbeat = state_report.payload or {}, heartbeat_report.payload or {}
        if state.get("pid") != pid or state.get("process_identity") != identity:
            return self._health_unconfirmed("gateway_state_identity_unconfirmed", quiet=quiet_unconfirmed)
        started = state.get("started_at")
        if (type(started) not in (int, float) or started <= 0 or started > time.time()
                or (type(started) is float and not math.isfinite(started))):
            return self._health_unconfirmed("gateway_start_unconfirmed", quiet=quiet_unconfirmed)
        if heartbeat.get("pid") != pid or heartbeat.get("started_at") != started:
            return self._health_unconfirmed("gateway_heartbeat_identity_unconfirmed", quiet=quiet_unconfirmed)
        if heartbeat.get("status") != "running" or not self._heartbeat_is_fresh(heartbeat):
            return self._health_unconfirmed("gateway_heartbeat_not_running", quiet=quiet_unconfirmed)
        if heartbeat.get("dispatcher_alive") is not True or heartbeat.get("dispatcher_state") != "running":
            return self._health_unconfirmed("gateway_dispatcher_unconfirmed", quiet=quiet_unconfirmed)
        if not gateway_readiness(self._paths, pid=pid, process_alive=True).ready:
            return self._health_unconfirmed("gateway_not_ready", quiet=quiet_unconfirmed)
        try:
            from .supervisor_generation import require_healthy_workers

            require_healthy_workers(self, state, time.time())
        except (OSError, ValueError):
            return self._health_unconfirmed("gateway_worker_generation_unconfirmed", quiet=quiet_unconfirmed)
        return True

    # LLM: 静音仍留本轮结构化原因但不刷日志；默认保运行期WARN，不写状态/清PID或授启动权。
    # 函数用途: 留下可行动的只读健康拒绝原因。
    def _health_unconfirmed(self, reason: str, *, quiet: bool = False) -> bool:
        error = {"context": "gateway.supervisor.health.read", "reason": reason,
                 "message": "Gateway身份或就绪资料未确认，请核原配置对应状态后正常重启。"}
        if quiet:
            self._last_health_load_errors.append(error)
        else:
            self._record_health_load_error(error)
        return False

    # LLM: 每轮诊断只写本实例列表及日志，不修改服务身份。
    # 函数用途: 留存本次健康读取错误。
    def _record_health_load_error(self, load_error: dict) -> None:
        record_health_load_error(
            self._last_health_load_errors,
            load_error,
            log_warn=self._log_warn,
        )

    # LLM: 只接受有限且非未来的原更新时间；fresh本身不是ready，仍须通过角色和本代绑定。
    # 函数用途: 按原timeout检查心跳年龄，不让NaN/未来时间伪装存活。
    def _heartbeat_is_fresh(self, heartbeat: dict | None) -> bool:
        updated_at = (heartbeat or {}).get("updated_at")
        now = time.time()
        if (type(updated_at) not in (int, float) or updated_at <= 0 or updated_at > now
                or (type(updated_at) is float and not math.isfinite(updated_at))):
            return False
        return now - updated_at <= self.heartbeat_timeout

    # LLM: 复用原PID/state读取；活进程不被自动恢复重启，坏记录保留诊断。
    # 函数用途: 检查适配器进程是否仍在运行。
    def _check_adapter_health(self) -> bool:
        self._resolve_agent_and_paths()

        # Check adapter PID file
        pid_report = get_running_pid_report(self._paths.adapter_pid)
        if pid_report.load_error is not None:
            self._record_health_load_error(pid_report.load_error)
            return False
        pid = pid_report.pid
        if not pid:
            return False

        if not is_pid_alive(pid):
            return False

        state_path = self._paths.root / "adapter_state.json"
        state_report = _read_adapter_state_report(state_path)
        if state_report.load_error is not None:
            self._record_health_load_error(state_report.load_error)
            return False
        state = state_report.payload or {}
        if state.get("state") == "running":
            return True

        return True  # PID alive, assume healthy if no state file

    # LLM: 委托同一维护与原代际启动门，不额外创建进程。
    # 函数用途: 尝试启动并等待Gateway就绪。
    def _start_gateway(self) -> int | None:
        return start_gateway(self)

    # LLM: 只运输原stop参数及可选冻结身份，默认调用保持原语义，不授关闭证明。
    # 函数用途: 让恢复沿同一停止入口保持准确旧代。
    def _stop_gateway(self, timeout: float = 20.0, *, expected_identity: dict | None = None) -> bool:
        return stop_gateway(self, timeout=timeout, expected_identity=expected_identity)

    # LLM: 保留同一维护/冷却/代际关闭门，不旁路原停止流程。
    # 函数用途: 请求恢复故障Gateway。
    def _restart_gateway(self) -> bool:
        return restart_gateway(self)

    # LLM: 始终委托同一维护/意图/PID/冷却读门，不另造启动渠道。
    # 函数用途: 尝试恢复崩溃的适配器并返回是否发起启动。
    def _restart_adapter(self) -> bool:
        return restart_adapter(self)

    # LLM: 信号只设置退出意图，实际收尾由原主循环完成。
    # 函数用途: 收到终止信号后让监护循环有序退出。
    def _handle_signal(self, signum, frame) -> None:
        sig_name = signal.Signals(signum).name
        self._log_info(f"Received {sig_name}, initiating graceful shutdown...")
        self._stop_requested = True

    # LLM: 入口委托原循环，PID和Gateway角色记录仍分离。
    # 函数用途: 开始监护并返回退出码。
    def run(self) -> int:
        return run_supervisor_loop(self)


# LLM: CLI必须交同一实际Agent及其config_path；选项只管监护节奏，不接factory/callback，不改变原运行循环副作用。
# 函数用途: 使用已装配Agent运行原监护循环，不二次初始化业务home或解析工作区。
def run_supervisor(
    config_path: str,
    options: SupervisorConfig | None = None,
    *,
    agent,
    heartbeat_timeout: float = 120.0,
    check_interval: float = 10.0,
    max_restart_attempts: int = 5,
    restart_cooldown: float = 30.0,
    log_path: Path | None = None,
) -> int:
    if options is None:
        options = SupervisorConfig(
            heartbeat_timeout=heartbeat_timeout,
            check_interval=check_interval,
            max_restart_attempts=max_restart_attempts,
            restart_cooldown=restart_cooldown,
            log_path=log_path,
        )
    supervisor = GatewaySupervisor(config_path, agent=agent, options=options)
    return supervisor.run()


# LLM: 纯数字旧PID只供保守存活发现，不能升级为身份已验；不清理任何记录，坏/未知资料阻止重复启动。
# 函数用途: 给状态与启动命令读取独立监护身份，明确区分存活和同代可核验。
def read_supervisor_process_report(pid_path: Path) -> SupervisorPayloadReport:
    if not pid_path.exists():
        return SupervisorPayloadReport(None)
    record = read_pid_record_report(pid_path)
    if record.load_error is not None:
        pid = read_pid_file(pid_path)
        if pid and is_pid_alive(pid):
            return SupervisorPayloadReport({"pid": pid, "alive": True, "identity_verified": False},
                                           {"reason": "legacy_supervisor_identity_unconfirmed"})
        return SupervisorPayloadReport(None, record.load_error)
    value = record.payload or {}
    identity = {key: value.get(key) for key in ("host_id", "pid", "start_time")}
    if value.get("kind") != "my-agent-supervisor":
        return SupervisorPayloadReport(None, {"reason": "supervisor_role_unconfirmed"})
    live = process_identity_is_live(identity)
    if live is False:
        return SupervisorPayloadReport({"pid": identity["pid"], "alive": False, "identity_verified": True})
    return SupervisorPayloadReport({"pid": identity["pid"], "alive": live is True, "identity_verified": live is True},
                                   None if live is True else {"reason": "supervisor_identity_unconfirmed"})


# LLM: 路径仅由CLI同一实际Agent的gateway_paths传入；这里只读原supervisor身份，不能据监护者存活签Gateway ready。
# 函数用途: 检查所选规范路径的监护进程，不解析配置或初始化Agent。
def is_supervisor_running(paths: GatewayPaths) -> bool:
    report = read_supervisor_process_report(paths.supervisor_pid)
    return report.load_error is not None or bool((report.payload or {}).get("alive"))


# LLM: CLI传同Agent规范GatewayPaths；保原目标PID/TERM/等待语义，不在此重建配置、Agent或角色权限。
# 函数用途: 正常停止所选规范路径的监护进程并等待原超时。
def stop_supervisor(paths: GatewayPaths, timeout: float = 10.0) -> bool:
    pid = read_pid_file(paths.supervisor_pid)
    if not pid or not is_pid_alive(pid):
        return True

    terminate_pid(pid)
    return wait_for_pid_exit(pid, timeout)


# LLM: 仅保留旧路径投影接口，不据workspace授予角色或配置权威。
# 函数用途: 返回规范候选列表中的首个工作目录。
def _primary_workspace_root(raw_root: object, default: Path) -> Path:
    return _workspace_roots(raw_root, default)[0]


# LLM: 仅规范化并去重输入，不创建路径或解析业务配置。
# 函数用途: 把单个或多个工作目录转换为稳定列表。
def _workspace_roots(raw_root: object, default: Path) -> list[Path]:
    if isinstance(raw_root, list):
        values = raw_root or [default]
    else:
        values = [raw_root or default]
    roots: list[Path] = []
    for item in values:
        path = Path(str(item or default)).expanduser().resolve()
        if path not in roots:
            roots.append(path)
    return roots or [default.resolve()]
