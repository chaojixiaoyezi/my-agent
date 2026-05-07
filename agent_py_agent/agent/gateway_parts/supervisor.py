# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""gateway watchdog supervisor — monitors gateway health and auto-restarts on crash.

这个文件实现一个独立的后台监督进程，负责监控 gateway 是否崩溃，并在崩溃后自动拉起。
不依赖 systemd，在 macOS/Linux 上都能跑。
"""

import json
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    WriteRuntimeStatusParams,
    acquire_scoped_lock,
    get_running_pid,
    read_pid_file,
    read_runtime_status,
    release_scoped_lock,
    remove_pid_file,
    write_pid_file,
    write_runtime_status,
)
from .io import read_json_file
from .paths import gateway_paths
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit
from .supervisor_runtime import (
    restart_gateway as _restart_gateway_impl,
)
from .supervisor_runtime import (
    run_supervisor_loop,
)
from .supervisor_runtime import (
    start_gateway as _start_gateway_impl,
)
from .supervisor_runtime import (
    stop_gateway as _stop_gateway_impl,
)


# LLM: _utc_now_iso 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理utcnowiso相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM: _read_adapter_state 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询adapter状态需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _read_adapter_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


# LLM: SupervisorConfig 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存监督器config字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发请求队列、租约文件、进程状态和响应渲染相关副作用，需保持公开契约稳定。
@dataclass
class SupervisorConfig:

    workspace_root: str | None = None
    heartbeat_timeout: float = 120.0
    check_interval: float = 10.0
    max_restart_attempts: int = 5
    restart_cooldown: float = 30.0
    log_path: Path | None = None


# LLM: GatewaySupervisor 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 封装网关监督器相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发请求队列、租约文件、进程状态和响应渲染相关副作用，需保持公开契约稳定。
class GatewaySupervisor:

    # LLM: __init__ 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
    def __init__(
        self,
        config_path: str,
        *,
        options: SupervisorConfig | None = None,
    ):
        self.config_path = config_path
        _opts = options or SupervisorConfig()
        self.workspace_root = _opts.workspace_root
        self.heartbeat_timeout = _opts.heartbeat_timeout
        self.check_interval = _opts.check_interval
        self.max_restart_attempts = _opts.max_restart_attempts
        self.restart_cooldown = _opts.restart_cooldown
        self.log_path = _opts.log_path

        self._supervisor_pid: int = os.getpid()
        self._gateway_pid: int | None = None
        self._restart_count: int = 0
        self._last_restart_at: float = 0.0
        self._stop_requested: bool = False
        self._agent = None
        self._paths = None

    # LLM: _resolve_agent_and_paths 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 读取或查询agent路径需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _resolve_agent_and_paths(self):
        if self._agent is not None or self._paths is not None:
            return

        # Import lazily to avoid circular dependencies
        from ..config import load_config
        from ..core import SimpleAgent

        config = load_config(self.config_path)
        roots = _workspace_roots(config.workspace_root, Path(self.config_path).resolve().parent)
        root = roots[0]
        if self.workspace_root:
            root = Path(self.workspace_root).resolve()
            roots = [root]

        self._agent = SimpleAgent(config, root, workspace_roots=roots)
        self._paths = gateway_paths(self._agent)

    # LLM: _log 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 写入log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        print(line, flush=True)
        self._append_log_file(line)

    # LLM: _append_log_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 写入文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
    def _append_log_file(self, line: str) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # LLM: _log_info 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 写入info的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
    def _log_info(self, msg: str) -> None:
        self._log("INFO", msg)

    # LLM: _log_warn 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 写入warn的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
    def _log_warn(self, msg: str) -> None:
        self._log("WARN", msg)

    # LLM: _log_error 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 写入error的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
    def _log_error(self, msg: str) -> None:
        self._log("ERROR", msg)

    # LLM: _read_gateway_heartbeat 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 读取或查询网关heartbeat需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _read_gateway_heartbeat(self) -> dict | None:
        self._resolve_agent_and_paths()
        heartbeat_path = self._paths.heartbeat
        if not heartbeat_path.exists():
            return None
        try:
            return json.loads(heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # LLM: _is_gateway_healthy 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 判断网关healthy条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _is_gateway_healthy(self) -> bool:
        self._resolve_agent_and_paths()

        # Check heartbeat freshness first (most reliable)
        heartbeat = self._read_gateway_heartbeat()
        if self._heartbeat_is_fresh(heartbeat):
            return True

        # Fallback: check PID file directly
        pid = get_running_pid(self._paths.pid)
        if not pid:
            return False

        if not is_pid_alive(pid):
            return False

        # PID file is fresh but no heartbeat - could be startup phase
        if not heartbeat:
            self._log_info("Gateway PID alive but no heartbeat yet (startup phase)")
            return True

        return False

    # LLM: _heartbeat_is_fresh 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 处理heartbeatisfresh相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _heartbeat_is_fresh(self, heartbeat: dict | None) -> bool:
        if not heartbeat:
            return False
        updated_at = heartbeat.get("updated_at", 0)
        if not updated_at:
            return False
        age = time.time() - float(updated_at)
        if age <= self.heartbeat_timeout:
            return True
        self._log_warn(f"Gateway heartbeat stale: age={age:.1f}s > {self.heartbeat_timeout}s")
        return False

    # LLM: _check_adapter_health 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 校验adapterhealth需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _check_adapter_health(self) -> bool:
        self._resolve_agent_and_paths()

        # Check adapter PID file
        pid = get_running_pid(self._paths.adapter_pid)
        if not pid:
            return False

        if not is_pid_alive(pid):
            return False

        state_path = self._paths.root / "adapter_state.json"
        state = _read_adapter_state(state_path)
        if state.get("state") == "running":
            return True

        return True  # PID alive, assume healthy if no state file

    # LLM: _start_gateway 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进网关的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _start_gateway(self) -> int | None:
        return _start_gateway_impl(self)

    # LLM: _stop_gateway 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进网关的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _stop_gateway(self, timeout: float = 20.0) -> bool:
        return _stop_gateway_impl(self, timeout=timeout)

    # LLM: _restart_gateway 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进网关的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _restart_gateway(self) -> bool:
        return _restart_gateway_impl(self)

    # LLM: _handle_signal 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进signal的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_signal(self, signum, frame) -> None:
        sig_name = signal.Signals(signum).name
        self._log_info(f"Received {sig_name}, initiating graceful shutdown...")
        self._stop_requested = True

    # LLM: run 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进run的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def run(self) -> int:
        return run_supervisor_loop(self)


# LLM: run_supervisor 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进监督器的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def run_supervisor(
    config_path: str,
    options: SupervisorConfig | None = None,
    *,
    workspace_root: str | None = None,
    heartbeat_timeout: float = 120.0,
    check_interval: float = 10.0,
    max_restart_attempts: int = 5,
    restart_cooldown: float = 30.0,
    log_path: Path | None = None,
) -> int:
    if options is None:
        options = SupervisorConfig(
            workspace_root=workspace_root,
            heartbeat_timeout=heartbeat_timeout,
            check_interval=check_interval,
            max_restart_attempts=max_restart_attempts,
            restart_cooldown=restart_cooldown,
            log_path=log_path,
        )
    supervisor = GatewaySupervisor(config_path, options=options)
    return supervisor.run()


# LLM: is_supervisor_running 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 判断监督器running条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def is_supervisor_running(config_path: str) -> bool:
    from ..config import load_config
    from ..core import SimpleAgent

    config = load_config(config_path)
    roots = _workspace_roots(config.workspace_root, Path(config_path).resolve().parent)
    agent = SimpleAgent(config, roots[0], workspace_roots=roots)
    paths = gateway_paths(agent)
    supervisor_pid_path = paths.root / "supervisor.pid"

    if not supervisor_pid_path.exists():
        return False

    pid = read_pid_file(supervisor_pid_path)
    if not pid:
        return False

    return is_pid_alive(pid)


# LLM: stop_supervisor 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进监督器的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def stop_supervisor(config_path: str, timeout: float = 10.0) -> bool:
    from ..config import load_config
    from ..core import SimpleAgent

    config = load_config(config_path)
    roots = _workspace_roots(config.workspace_root, Path(config_path).resolve().parent)
    agent = SimpleAgent(config, roots[0], workspace_roots=roots)
    paths = gateway_paths(agent)
    supervisor_pid_path = paths.root / "supervisor.pid"

    pid = read_pid_file(supervisor_pid_path)
    if not pid or not is_pid_alive(pid):
        return True

    terminate_pid(pid)
    return wait_for_pid_exit(pid, timeout)


# LLM: _primary_workspace_root 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理primaryworkspaceroot相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _primary_workspace_root(raw_root: object, default: Path) -> Path:
    return _workspace_roots(raw_root, default)[0]


# LLM: _workspace_roots 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理workspaceroots相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
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
