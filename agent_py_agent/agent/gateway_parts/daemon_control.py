
from __future__ import annotations

"""Daemon control: background fork, PID files, graceful shutdown, and scoped locks."""

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..runtime_errors import runtime_error_report
from .daemon_metadata import (
    _build_pid_record,
    _flocked_sidecar,
    _get_process_start_time,
    _read_json_file,
    _scope_hash,
    _utc_now_iso,
    _write_json_file,
    build_process_identity,
)
from .process_control import is_pid_alive as _is_pid_alive
from .process_control import wait_for_pid_exit as _wait_for_pid_exit
from .runtime_status import WriteRuntimeStatusParams, read_runtime_status, write_runtime_status
from .scoped_locks import (
    _get_lock_dir,
    _get_scope_lock_path,
    _release_lock_if_stale,
    acquire_scoped_lock,
    release_all_scoped_locks,
    release_scoped_lock,
)

# Exit code to signal service manager should restart (长期助手: EX_TEMPFAIL = 75)
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75


@dataclass(frozen=True)
class PidRecordReadReport:
    payload: dict | None
    load_error: dict | None = None


@dataclass(frozen=True)
class RunningPidReport:
    pid: int | None
    load_error: dict | None = None


# 鈹€鈹€ PID file management 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def read_pid_file(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        content = pid_path.read_text(encoding="utf-8").strip()
    except (ValueError, OSError):
        return None
    return _pid_from_file_content(pid_path, content)


def _pid_from_file_content(pid_path: Path, content: str) -> int | None:
    if not content:
        return None
    if content.startswith("{"):
        return _pid_from_record(_read_json_file(pid_path))
    try:
        return int(content)
    except ValueError:
        return None


def _pid_from_record(record: dict | None) -> int | None:
    if not record or "pid" not in record:
        return None
    try:
        return int(record["pid"])
    except (TypeError, ValueError):
        return None


def remove_pid_file(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except OSError:
        pass


def check_already_running(pid_path: Path) -> tuple[bool, int | None]:
    existing_pid = read_pid_file(pid_path)
    if existing_pid is None:
        return False, None
    if _is_pid_alive(existing_pid):
        return True, existing_pid
    # Stale PID file - process is dead
    return False, None


# 鈹€鈹€ PID record with start time (长期助手 pattern) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def write_pid_record(pid_path: Path) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_file(pid_path, _build_pid_record())


def read_pid_record(pid_path: Path) -> dict | None:
    return _read_json_file(pid_path)


def read_pid_record_report(pid_path: Path) -> PidRecordReadReport:
    if not pid_path.exists():
        return PidRecordReadReport(None)
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return PidRecordReadReport(None, _pid_record_load_error(pid_path, exc))
    if not raw:
        return PidRecordReadReport(None)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return PidRecordReadReport(None, _pid_record_load_error(pid_path, exc))
    if isinstance(payload, dict):
        return PidRecordReadReport(payload)
    return PidRecordReadReport(
        None,
        _pid_record_load_error(
            pid_path,
            ValueError(f"PID record root is {type(payload).__name__}, expected object"),
        ),
    )


def _pid_record_load_error(pid_path: Path, exc: BaseException) -> dict:
    report = runtime_error_report(exc, context="gateway.pid_record.read")
    report["path"] = str(pid_path)
    return report


def get_running_pid(pid_path: Path, *, cleanup_stale: bool = True) -> int | None:
    return get_running_pid_report(pid_path, cleanup_stale=cleanup_stale).pid


def get_running_pid_report(pid_path: Path, *, cleanup_stale: bool = True) -> RunningPidReport:
    record_report = read_pid_record_report(pid_path)
    if record_report.load_error is not None:
        return RunningPidReport(None, record_report.load_error)
    record = record_report.payload
    if not record:
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return RunningPidReport(None)

    pid = _pid_from_record(record)
    if pid is None:
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return RunningPidReport(None)

    # while macOS/Linux keep the POSIX liveness path.
    if not _is_pid_alive(pid):
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return RunningPidReport(None)

    # Check start_time to detect PID reuse
    recorded_start = record.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and current_start != recorded_start:
        # PID was reused by another process
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return RunningPidReport(None)

    return RunningPidReport(pid)


def _cleanup_stale_pid_file(pid_path: Path, cleanup_stale: bool) -> None:
    if not cleanup_stale:
        return
    try:
        pid_path.unlink()
    except OSError:
        pass


def remove_pid_file_if_owned(pid_path: Path) -> None:
    try:
        record_report = read_pid_record_report(pid_path)
        if record_report.load_error is not None:
            return
        record = record_report.payload
        file_pid = _pid_from_record(record)
        if file_pid is not None and file_pid != os.getpid():
            return
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass


# LLM: Gateway stop markers must name the exact process generation. Resolve the durable PID
# record first, then fill any startup-era missing identity fields from the live process facts.
# 函数用途: 取得一次停止请求要锁定的 Gateway 进程身份，避免 PID 复用或快速重启串代。
def gateway_process_identity(pid_path: Path, pid: int | None = None) -> dict[str, object] | None:
    target_pid = int(pid or 0)
    record_report = read_pid_record_report(pid_path)
    record = record_report.payload or {}
    record_pid = _pid_from_record(record)
    if target_pid <= 0:
        target_pid = int(record_pid or 0)
    if target_pid <= 0:
        return None
    identity = build_process_identity(target_pid)
    if record_pid == target_pid:
        for key in ("host_id", "start_time"):
            if record.get(key) is not None:
                identity[key] = record[key]
    return identity


# LLM: A stop marker is authoritative only for its exact target process. Legacy unscoped markers
# are accepted solely when their timestamp belongs to this lifecycle; malformed old files cannot
# terminate a fresh Gateway. Keep this predicate shared by the wait loop, classifier, and cleanup.
# 函数用途: 判断停止文件是否确实发给当前这一次 Gateway，而不是上一次进程的遗留文件。
def stop_request_targets_process(
    payload: object,
    process_identity: object,
    *,
    process_started_at: float = 0.0,
) -> bool:
    if not isinstance(payload, dict) or not isinstance(process_identity, dict):
        return False
    target = payload.get("target_process")
    if isinstance(target, dict):
        try:
            target_pid = int(target.get("pid") or 0)
            process_pid = int(process_identity.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if target_pid <= 0 or process_pid <= 0 or target_pid != process_pid:
            return False
        target_host = str(target.get("host_id") or "")
        process_host = str(process_identity.get("host_id") or "")
        if target_host and process_host and target_host != process_host:
            return False
        target_start = target.get("start_time")
        process_start = process_identity.get("start_time")
        if target_start is not None and process_start is not None:
            return target_start == process_start
        return _stop_request_is_new_enough(payload, process_started_at)
    return _stop_request_is_new_enough(payload, process_started_at)


# LLM: Timestamp fallback exists only for legacy or platforms without process start time. A
# production lifecycle with a known start rejects missing/malformed/older timestamps; tests and
# legacy embedders that do not provide a lifecycle start retain presence-based compatibility.
# 函数用途: 在缺少完整进程指纹时，用请求时间判断停止文件是不是本轮新写的。
def _stop_request_is_new_enough(payload: dict[str, object], process_started_at: float) -> bool:
    if process_started_at <= 0:
        return True
    try:
        requested_at = float(payload.get("requested_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return requested_at >= process_started_at


# LLM: All external Gateway stop writers use one atomic target-bound contract. The caller may
# supply the already-resolved PID, but the persisted identity always includes PID/start-time/host.
# 函数用途: 原子写入只对指定 Gateway 进程生效的停止请求，并返回落盘内容供日志复用。
def write_targeted_gateway_stop_request(
    pid_path: Path,
    stop_request_path: Path,
    *,
    reason: str,
    target_pid: int | None = None,
    source: str = "",
) -> dict[str, object] | None:
    identity = gateway_process_identity(pid_path, target_pid)
    if identity is None:
        return None
    payload: dict[str, object] = {
        "requested_at": time.time(),
        "reason": reason,
        "target_process": identity,
    }
    if source:
        payload["source"] = source
    _write_json_file(stop_request_path, payload)
    return payload


# LLM: Cleanup must compare and unlink under the same sidecar lock used by atomic writers. An old
# Gateway is forbidden from deleting a stop request that was concurrently written for its successor.
# 函数用途: 仅当停止文件属于指定进程代次时删除，消除快速重启时的跨进程清理竞态。
def remove_gateway_stop_request_if_owned(
    stop_request_path: Path,
    process_identity: object,
    *,
    process_started_at: float = 0.0,
) -> bool:
    with _flocked_sidecar(stop_request_path):
        payload = _read_json_file(stop_request_path)
        if not stop_request_targets_process(
            payload,
            process_identity,
            process_started_at=process_started_at,
        ):
            return False
        try:
            stop_request_path.unlink()
        except FileNotFoundError:
            return False
        return True


# Public daemon control helpers.


def write_pid_file(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def daemonize(pid_path: Path) -> bool:
    if os.name == "nt":
        return False

    pid = os.fork()
    if pid > 0:
        # Parent - wait for child to confirm startup, then exit
        time.sleep(0.5)
        # Check if child wrote its PID
        child_pid = read_pid_file(pid_path)
        if child_pid and _is_pid_alive(child_pid):
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


# LLM: Legacy daemon callers still receive a boolean, while the written stop record is now bound
# to the exact PID generation through the same canonical Gateway stop contract.
# 函数用途: 向当前存活的 Gateway 写入可核验的优雅停止请求。
def request_graceful_shutdown(
    pid_path: Path, stop_request_path: Path, reason: str = "user request"
) -> bool:
    existing_pid = read_pid_file(pid_path)
    if not existing_pid or not _is_pid_alive(existing_pid):
        return False
    return write_targeted_gateway_stop_request(
        pid_path,
        stop_request_path,
        reason=reason,
        target_pid=existing_pid,
        source="daemon_control",
    ) is not None


def wait_for_shutdown(pid: int, timeout: float) -> bool:
    return _wait_for_pid_exit(pid, timeout)


def install_signal_handler(handler) -> None:
    if os.name == "nt":
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    else:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
