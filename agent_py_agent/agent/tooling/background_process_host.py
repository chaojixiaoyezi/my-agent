from __future__ import annotations

"""Detached host for one managed background command."""

# LLM: A one-shot subagent runner must never be the OS parent that owns a durable
# background command. This small host survives the runner, keeps bwrap's
# --die-with-parent safety meaningful, enforces the log cap, and exits with the
# command. The durable authorization record remains in process_session_store.
# 模块用途: 为每条显式后台命令提供一个独立托管进程；子代理结束后服务继续运行，
# 但托管进程退出时沙箱和全部后代仍会自动收口。

import argparse
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import read_json_object_report, write_json_file_atomic

HOST_STATE_SCHEMA = "background_process_host.v1"
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 3.0
_HOST_POLL_SECONDS = 0.2


# LLM: The caller retains only the host Popen for same-process fast status; the
# state file is the cross-process exit-code handoff and child_pid is diagnostic.
# 类用途: 返回已启动托管进程、其状态文件和真实沙箱根进程号。
@dataclass(frozen=True)
class HostedBackgroundProcess:
    process: subprocess.Popen
    state_file: Path
    child_pid: int
    pid_birth_token: str


# LLM: The launch spec is a private host-side handoff, not model authority. Never
# interpolate argv through a shell; preserve the already-built sandbox argv exactly.
# 函数用途: 启动独立托管进程，并等待它确认沙箱命令已经真实创建。
def start_background_process_host(
    command_argv: list[str],
    *,
    cwd: Path,
    log_path: Path,
    env: dict[str, str],
    max_log_bytes: int,
    startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> HostedBackgroundProcess:
    if not command_argv or not all(isinstance(item, str) and item for item in command_argv):
        raise ValueError("managed background command argv is empty or invalid")
    jobs_dir = log_path.parent
    jobs_dir.mkdir(parents=True, exist_ok=True)
    token = f"{time.time_ns()}-{uuid.uuid4().hex[:10]}"
    spec_path = jobs_dir / f"host-{token}.launch.json"
    state_path = jobs_dir / f"host-{token}.state.json"
    write_json_file_atomic(
        spec_path,
        {
            "schema": HOST_STATE_SCHEMA,
            "command_argv": list(command_argv),
            "cwd": str(cwd),
            "log_path": str(log_path),
            "max_log_bytes": max(0, int(max_log_bytes)),
        },
    )
    try:
        spec_path.chmod(0o600)
    except OSError:
        pass
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "agent_py_agent.agent.tooling.background_process_host",
            "--spec",
            str(spec_path),
            "--state",
            str(state_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(env),
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    from .process_registry import capture_process_birth_token

    pid_birth_token = capture_process_birth_token(process.pid)
    if not pid_birth_token:
        _stop_failed_host(process)
        raise OSError("managed background host process identity is unavailable")
    state = _wait_for_startup(process, state_path, startup_timeout_seconds)
    if str(state.get("status") or "") not in {"running", "exited"}:
        _stop_failed_host(process)
        detail = str(state.get("error_type") or "host_startup_failed")
        raise OSError(f"managed background host failed to start command: {detail}")
    return HostedBackgroundProcess(
        process=process,
        state_file=state_path,
        child_pid=int(state.get("child_pid") or 0),
        pid_birth_token=pid_birth_token,
    )


# LLM: Startup waits only for a local state-file handshake and the host PID. It is
# bounded and never becomes a model-visible polling loop.
# 函数用途: 在很短的固定期限内等待 host 写出 started/failed 握手。
def _wait_for_startup(
    process: subprocess.Popen,
    state_path: Path,
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        report = read_json_object_report(
            state_path,
            context="background_process_host.startup_state",
        )
        status = str(report.payload.get("status") or "")
        if status in {"running", "failed", "exited"}:
            return report.payload
        if process.poll() is not None:
            break
        time.sleep(0.01)
    report = read_json_object_report(
        state_path,
        context="background_process_host.startup_state_final",
    )
    if report.payload:
        return report.payload
    return {"schema": HOST_STATE_SCHEMA, "status": "failed", "error_type": "startup_timeout"}


# LLM: Failed startup cleanup targets the host tree, never a shell-parsed PID.
# 函数用途: host 未完成握手时尽快回收它，避免留下半启动进程。
def _stop_failed_host(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    from .process_registry import terminate_process_tree

    terminate_process_tree(process.pid, process, grace_seconds=0.5)


# LLM: The host validates the file schema and argv types before executing anything.
# It deletes the one-shot launch spec after reading so secrets do not linger there.
# 函数用途: 托管子进程读取一次性启动参数，并运行、监控真实沙箱命令。
def run_background_process_host(spec_path: Path, state_path: Path) -> int:
    child: subprocess.Popen | None = None
    try:
        spec = _read_launch_spec(spec_path)
        try:
            spec_path.unlink(missing_ok=True)
        except OSError:
            pass
        command_argv = [str(item) for item in spec["command_argv"]]
        cwd = Path(str(spec["cwd"])).expanduser().resolve(strict=False)
        log_path = Path(str(spec["log_path"])).expanduser().resolve(strict=False)
        max_log_bytes = max(0, int(spec.get("max_log_bytes") or 0))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_handle:
            child = subprocess.Popen(
                command_argv,
                shell=False,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=dict(os.environ),
            )
            _write_host_state(
                state_path,
                status="running",
                child_pid=child.pid,
                started_at=time.time(),
            )
            return _monitor_child(child, log_path, state_path, max_log_bytes)
    except Exception as exc:  # noqa: BLE001 - child host must always publish a typed startup failure.
        if child is not None and child.poll() is None:
            from .process_registry import terminate_process_tree

            terminate_process_tree(child.pid, child, grace_seconds=0.5)
        _write_host_state(
            state_path,
            status="failed",
            error_type=type(exc).__name__,
            finished_at=time.time(),
        )
        return 1
    finally:
        try:
            spec_path.unlink(missing_ok=True)
        except OSError:
            pass


# LLM: Only the exact schema and a non-empty string argv are executable. Paths are
# opaque host inputs already selected by ShellTool and are not repaired here.
# 函数用途: 校验托管启动文件，损坏或类型不符时拒绝启动任何命令。
def _read_launch_spec(path: Path) -> dict[str, object]:
    report = read_json_object_report(path, context="background_process_host.launch_spec")
    payload = report.payload
    if report.load_error is not None or str(payload.get("schema") or "") != HOST_STATE_SCHEMA:
        raise ValueError("invalid managed background host launch schema")
    argv = payload.get("command_argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise ValueError("invalid managed background host argv")
    if not str(payload.get("cwd") or "") or not str(payload.get("log_path") or ""):
        raise ValueError("managed background host paths are required")
    return payload


# LLM: The monitor owns log-cap enforcement after the launching agent disappears.
# Child exit is copied to the state file before the host exits.
# 函数用途: 等待真实命令结束，持续限制日志大小，并把真实退出码留给其他进程查询。
def _monitor_child(
    child: subprocess.Popen,
    log_path: Path,
    state_path: Path,
    max_log_bytes: int,
) -> int:
    reason = "command_exited"
    while child.poll() is None:
        if max_log_bytes > 0 and _log_size(log_path) > max_log_bytes:
            from .process_registry import terminate_process_tree

            terminate_process_tree(child.pid, child, grace_seconds=0.5)
            reason = "log_limit_exceeded"
            break
        time.sleep(_HOST_POLL_SECONDS)
    try:
        return_code = int(child.wait(timeout=1))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return_code = int(child.poll() if child.poll() is not None else -1)
    _write_host_state(
        state_path,
        status="exited",
        child_pid=child.pid,
        exit_code=return_code,
        reason=reason,
        finished_at=time.time(),
    )
    return return_code


# LLM: Log-stat failure is observational and must not kill an otherwise healthy
# process; the next poll can recover.
# 函数用途: 安全读取当前后台日志字节数，暂时读不到时按 0 处理。
def _log_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


# LLM: Host state is diagnostic lifecycle data, never an authorization source.
# Atomic replacement prevents readers from observing a torn JSON document.
# 函数用途: 原子写出 host 的 running/failed/exited 状态与真实子进程号。
def _write_host_state(state_path: Path, *, status: str, **fields: object) -> None:
    payload = {"schema": HOST_STATE_SCHEMA, "status": str(status), **fields}
    try:
        write_json_file_atomic(state_path, payload)
        state_path.chmod(0o600)
    except OSError:
        pass


# LLM: CLI accepts only host-internal file paths. Product callers should use
# start_background_process_host rather than invoking this parser themselves.
# 函数用途: 解析托管子进程内部入口参数。
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--state", required=True)
    return parser.parse_args(argv)


# LLM: Keep module execution tiny so import failures and startup failures resolve
# to one process exit and, when possible, one typed state record.
# 函数用途: 作为 `python -m` 的后台托管进程入口。
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return run_background_process_host(
        Path(args.spec).expanduser().resolve(strict=False),
        Path(args.state).expanduser().resolve(strict=False),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HOST_STATE_SCHEMA",
    "HostedBackgroundProcess",
    "start_background_process_host",
    "run_background_process_host",
]
