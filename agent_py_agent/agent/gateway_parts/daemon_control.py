# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""daemon control - fork to background, PID file management, graceful shutdown, scoped locks.

缁欎汉鐪嬬殑瑙ｉ噴锛?
杩欎釜鏂囦欢澶勭悊 daemon 灞傞潰鐨勬帶鍒讹細鎬庝箞 fork 鍒板悗鍙般€佹€庝箞妫€娴嬮噸澶嶅惎鍔ㄣ€佹€庝箞浼橀泤鍏抽棴銆?
杩樺寘鍚?长期助手 椋庢牸鐨勫姛鑳斤細start time tracking 妫€娴?PID 閲嶇敤銆乻coped locks 闃叉澶氬疄渚嬪啿绐併€?
"""

import json
import os
import signal
import time
from pathlib import Path
from typing import Optional

from .daemon_metadata import (
    _build_pid_record,
    _get_process_start_time,
    _read_json_file,
    _scope_hash,
    _utc_now_iso,
    _write_json_file,
)

# Re-export from process_control for convenience
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit
from .runtime_status import WriteRuntimeStatusParams, read_runtime_status, write_runtime_status
from .scoped_locks import (
    _get_lock_dir,
    _get_scope_lock_path,
    _release_lock_if_stale,
    release_all_scoped_locks,
)
from .scoped_locks import (
    acquire_scoped_lock as _scoped_locks_acquire_scoped_lock,
)
from .scoped_locks import (
    release_scoped_lock as _scoped_locks_release_scoped_lock,
)

# Exit code to signal service manager should restart (长期助手: EX_TEMPFAIL = 75)
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75


# 鈹€鈹€ PID file management 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


# LLM: read_pid_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询pid文件需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def read_pid_file(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        content = pid_path.read_text(encoding="utf-8").strip()
    except (ValueError, OSError):
        return None
    return _pid_from_file_content(pid_path, content)


# LLM: _pid_from_file_content 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理来自pid文件内容相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _pid_from_file_content(pid_path: Path, content: str) -> int | None:
    if not content:
        return None
    if content.startswith("{"):
        return _pid_from_record(_read_json_file(pid_path))
    try:
        return int(content)
    except ValueError:
        return None


# LLM: _pid_from_record 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理来自pid记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _pid_from_record(record: dict | None) -> int | None:
    if not record or "pid" not in record:
        return None
    try:
        return int(record["pid"])
    except (TypeError, ValueError):
        return None


# LLM: remove_pid_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理removepid文件相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def remove_pid_file(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except OSError:
        pass


# LLM: check_already_running 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 校验alreadyrunning需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def check_already_running(pid_path: Path) -> tuple[bool, int | None]:
    existing_pid = read_pid_file(pid_path)
    if existing_pid is None:
        return False, None
    if is_pid_alive(existing_pid):
        return True, existing_pid
    # Stale PID file - process is dead
    return False, None


# 鈹€鈹€ PID record with start time (长期助手 pattern) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


# LLM: write_pid_record 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入pid记录的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def write_pid_record(pid_path: Path) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_file(pid_path, _build_pid_record())


# LLM: read_pid_record 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询pid记录需要的状态，返回调用方可继续处理的快照；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def read_pid_record(pid_path: Path) -> dict | None:
    return _read_json_file(pid_path)


# LLM: get_running_pid 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询runningpid需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def get_running_pid(pid_path: Path, *, cleanup_stale: bool = True) -> int | None:
    record = read_pid_record(pid_path)
    if not record:
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    pid = _pid_from_record(record)
    if pid is None:
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    # LLM: 使用共享进程控制辅助函数，避免 Windows 路径调用 POSIX 探测方式。
    # while macOS/Linux keep the POSIX liveness path.
    if not is_pid_alive(pid):
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    # Check start_time to detect PID reuse
    recorded_start = record.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and current_start != recorded_start:
        # PID was reused by another process
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    return pid


# LLM: _cleanup_stale_pid_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理cleanupstalepid文件相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _cleanup_stale_pid_file(pid_path: Path, cleanup_stale: bool) -> None:
    if not cleanup_stale:
        return
    try:
        pid_path.unlink()
    except OSError:
        pass


# LLM: remove_pid_file_if_owned 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理removepid文件ifowned相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def remove_pid_file_if_owned(pid_path: Path) -> None:
    try:
        record = _read_json_file(pid_path)
        file_pid = _pid_from_record(record)
        if file_pid is not None and file_pid != os.getpid():
            return
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass


# 鈹€鈹€ Scoped locks and runtime status are re-exported from focused modules. 鈹€鈹€鈹€


# LLM: acquire_scoped_lock preserves the legacy daemon_control patch point for lock-dir tests.
# 函数用途: 通过 daemon_control 暴露 scoped lock 获取，同时让本模块的 _get_lock_dir 覆盖生效。
def acquire_scoped_lock(
    scope: str, identity: str, metadata: dict[str, object] | None = None
) -> tuple[bool, dict | None]:
    return _with_scoped_lock_dir_override(
        lambda: _scoped_locks_acquire_scoped_lock(scope, identity, metadata)
    )


# LLM: release_scoped_lock preserves the legacy daemon_control patch point for lock-dir tests.
# 函数用途: 通过 daemon_control 暴露 scoped lock 释放，同时让本模块的 _get_lock_dir 覆盖生效。
def release_scoped_lock(scope: str, identity: str) -> None:
    _with_scoped_lock_dir_override(lambda: _scoped_locks_release_scoped_lock(scope, identity))


# LLM: _with_scoped_lock_dir_override bridges old facade tests to the focused scoped_locks module.
# 函数用途: 在一次调用期间把 scoped_locks 的锁目录解析切到 daemon_control 的可 patch 函数。
def _with_scoped_lock_dir_override(action):
    from . import scoped_locks

    original = scoped_locks._get_lock_dir
    scoped_locks._get_lock_dir = _get_lock_dir
    try:
        return action()
    finally:
        scoped_locks._get_lock_dir = original


# 鈹€鈹€ Legacy API compatibility 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


# LLM: write_pid_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入pid文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def write_pid_file(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


# LLM: daemonize 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理daemonize相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def daemonize(pid_path: Path) -> bool:
    if os.name == "nt":
        return False

    pid = os.fork()
    if pid > 0:
        # Parent - wait for child to confirm startup, then exit
        time.sleep(0.5)
        # Check if child wrote its PID
        child_pid = read_pid_file(pid_path)
        if child_pid and is_pid_alive(child_pid):
            return True  # Parent should exit
        return True

    # First child
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Second child - daemon
    # Redirect stdin/stdout/stderr to /dev/null
    devnull = os.open("/dev/null", os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    return False  # Continue as daemon


# LLM: request_graceful_shutdown 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送gracefulshutdown请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def request_graceful_shutdown(
    pid_path: Path, stop_request_path: Path, reason: str = "user request"
) -> bool:
    existing_pid = read_pid_file(pid_path)
    if not existing_pid or not is_pid_alive(existing_pid):
        return False

    # Write stop request
    stop_request_path.parent.mkdir(parents=True, exist_ok=True)
    stop_request_path.write_text(
        json.dumps({"requested_at": time.time(), "reason": reason}, ensure_ascii=False),
        encoding="utf-8",
    )
    return True


# LLM: wait_for_shutdown 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进shutdown的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def wait_for_shutdown(pid: int, timeout: float) -> bool:
    return wait_for_pid_exit(pid, timeout)


# LLM: install_signal_handler 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理installsignalhandler相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def install_signal_handler(handler) -> None:
    if os.name == "nt":
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    else:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
