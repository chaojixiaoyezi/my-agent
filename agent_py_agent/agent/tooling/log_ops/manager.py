
from __future__ import annotations

"""daemon 进程生命周期管理 —— 起/停/存活探测,经 PID 文件解耦于 agent 进程。

为什么用独立进程而非进程内线程(关键设计):
  需求是"撑数天数月"。进程内线程随 agent 进程退出就没了,扛不住长跑;而独立 daemon 进程
  用 start_new_session 起,agent 进程退出后它照跑。start/stop/status 不靠内存里的 Popen 句柄
  (agent 重启句柄就丢),而是靠落盘的 daemon.json(PID + 心跳)+ 按 PID 存活探测 —— 这样
  agent 进程重启后仍能找到、查、停之前起的 daemon(状态文件保证续接不丢)。

崩溃可重启:daemon 崩了,collector 的 offset/已处理集合/cursor 状态文件都在,manager 重新拉起
  daemon,run_collection_cycle 从断点续采,不重不丢。
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ...common import heartbeat
from .store import (
    CollectMetrics,
    LogOpsStore,
    SourceSpec,
    build_source_specs,
)

_IS_WINDOWS = os.name == "nt"
_KILL_GRACE_SECONDS = 4.0
# 心跳超过这个秒数没更新,认为 daemon 卡死/僵尸(即便 PID 还在),status 标 stale。
_HEARTBEAT_STALE_SECONDS = 60.0
_DEFAULT_POLL_INTERVAL = 2.0


def start_daemon(
    store: LogOpsStore,
    sources: list[str],
    *,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """写 config + 以独立进程拉起 daemon。已在跑则不重复起(幂等,返回 already_running)。

    返回 {status, pid, monitor_id, sources, ...}。sources 为空报错(没源没法监控)。
    """
    specs = build_source_specs(sources)
    if not specs:
        return {
            "ok": False,
            "error": "no_sources",
            "message": "log_monitor_start 需要至少一个有效源(文件路径/文件夹路径/API URL)。",
        }

    existing = daemon_liveness(store)
    if existing["alive"]:
        # 已在跑:合并/更新 config(支持加源),不重复起进程。
        store.write_config(specs, poll_interval_seconds=poll_interval_seconds)
        return _already_running_result(store, specs, existing["pid"])

    store.write_config(specs, poll_interval_seconds=poll_interval_seconds)
    _preinit_source_states(store, specs)
    process = _spawn_daemon(store, python_executable or sys.executable)
    if process is None:
        return {"ok": False, "error": "spawn_failed", "message": "daemon 进程启动失败。"}

    now = time.time()
    store.write_daemon(
        {
            "status": "running",
            "pid": process.pid,
            "poll_interval_seconds": poll_interval_seconds,
            "started_at": now,
            "heartbeat_at": now,
            "cycles": 0,
        }
    )
    return {
        "ok": True,
        "status": "started",
        "pid": process.pid,
        "monitor_id": store.monitor_id,
        "poll_interval_seconds": poll_interval_seconds,
        "sources": [spec.to_dict() for spec in specs],
        "root": str(store.root),
    }


def _preinit_source_states(store: LogOpsStore, specs: list[SourceSpec]) -> None:
    """预初始化各源状态文件(从空断点开始),保证 status 在第一拍前也能列出所有源。"""
    for spec in specs:
        if not store.state_path(spec.source_id).exists():
            store.write_state(spec.source_id, _initial_state_for(spec))


def _already_running_result(store: LogOpsStore, specs: list[SourceSpec], pid: int) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "already_running",
        "pid": pid,
        "monitor_id": store.monitor_id,
        "sources": [spec.to_dict() for spec in specs],
        "message": "daemon 已在运行,已更新监控源配置(无需重启)。",
    }


def stop_daemon(store: LogOpsStore) -> dict[str, Any]:
    """停 daemon:SIGTERM→宽限→SIGKILL,清 daemon.json。不在跑返回 not_running。"""
    record = store.read_daemon()
    pid = _record_pid(record)
    liveness = daemon_liveness(store)
    if not liveness["alive"]:
        store.clear_daemon()
        return {
            "ok": True,
            "status": "not_running",
            "monitor_id": store.monitor_id,
            "message": "daemon 当前没有在运行(或已退出)。",
        }

    signal_used = _terminate_pid(pid)
    store.write_daemon({**record, "status": "stopped", "stopped_at": time.time()})
    return {
        "ok": True,
        "status": "stopped",
        "pid": pid,
        "monitor_id": store.monitor_id,
        "signal": signal_used,
        "message": "已终止采集 daemon。状态文件保留,可随时 log_monitor_start 续接续采。",
    }


def daemon_liveness(store: LogOpsStore) -> dict[str, Any]:
    """探测 daemon 是否真活着:PID 存活 + 心跳不过期。返回 {alive, pid, reason, heartbeat_age}。"""
    record = store.read_daemon()
    pid = _record_pid(record)
    status = str(record.get("status") or "")
    if pid <= 0:
        return {"alive": False, "pid": 0, "reason": "no_pid", "status": status}
    if status == "stopped":
        return {"alive": False, "pid": pid, "reason": "stopped", "status": status}
    if not heartbeat.process_alive(pid, start_time=record.get("start_time")):
        return {"alive": False, "pid": pid, "reason": "process_gone", "status": status}
    hb_ts = float(record.get("heartbeat_at", 0) or 0)
    age = time.time() - hb_ts if hb_ts else None
    if age is not None and age > _HEARTBEAT_STALE_SECONDS:
        return {"alive": True, "pid": pid, "reason": "stale_heartbeat", "status": status, "heartbeat_age": age}
    return {"alive": True, "pid": pid, "reason": "ok", "status": status, "heartbeat_age": age}


def ensure_daemon_alive(store: LogOpsStore) -> dict[str, Any]:
    """看门狗自愈:曾起过的 daemon 进程异常消失(process_gone)就自动重拉,撑住"崩溃不中断采集"。
    保守边界:尊重用户主动 stop(stopped 不复活)、从没起过(no_pid)不擅自起、心跳僵死(stale)只提示不强杀。
    返回 {restarted, alive, ...}。借鉴 会话运行时 update_loop / 通道运行时 launchd KeepAlive 的进程自愈。"""
    live = daemon_liveness(store)
    if live["alive"]:
        return {"restarted": False, "alive": True, "pid": live.get("pid", 0), "reason": live.get("reason", "ok")}
    if live.get("reason") != "process_gone":
        return {"restarted": False, "alive": False, "reason": live.get("reason", "not_recoverable")}
    if not store.source_specs():
        return {"restarted": False, "alive": False, "reason": "never_configured"}
    process = _spawn_daemon(store, sys.executable)
    if process is None:
        return {"restarted": False, "alive": False, "reason": "respawn_failed"}
    return {"restarted": True, "alive": True, "pid": process.pid, "recovered_from": "process_gone"}


def reconciliation(store: LogOpsStore) -> dict[str, Any]:
    """不丢对账:逐源比对"已采集行数"与"存档行数",并汇总候选数。

    per_source[*]: collected_lines(采集计数) / archived_lines_metric(累计落档计数) /
                   archive_file_lines(存档文件真实行数) / consistent(三者是否一致)。
    不一致即异常信号(理论上 collected==archived==文件行数,因为每条采集到的都先落档)。
    """
    metrics = CollectMetrics.from_dict(store.read_metrics())
    per_source: dict[str, Any] = {}
    total_archive_file_lines = 0
    all_consistent = True
    specs = store.source_specs()
    known_ids = {spec.source_id for spec in specs} | set(metrics.per_source.keys())
    locator_by_id = {spec.source_id: spec.locator for spec in specs}
    kind_by_id = {spec.source_id: spec.kind for spec in specs}

    for source_id in sorted(known_ids):
        entry = metrics.per_source.get(source_id, {})
        collected = int(entry.get("collected_lines", 0) or 0)
        archived_metric = int(entry.get("archived_lines", 0) or 0)
        file_lines = store.count_archive_lines(source_id)
        total_archive_file_lines += file_lines
        consistent = collected == archived_metric == file_lines
        all_consistent = all_consistent and consistent
        per_source[source_id] = {
            "kind": kind_by_id.get(source_id, ""),
            "locator": locator_by_id.get(source_id, ""),
            "collected_lines": collected,
            "archived_lines_metric": archived_metric,
            "archive_file_lines": file_lines,
            "candidates": int(entry.get("candidates", 0) or 0),
            "cursor": entry.get("cursor"),
            "consistent": consistent,
            "last_error": entry.get("last_error", ""),
            "circuit": (entry.get("circuit") or {}).get("state", "closed"),
        }

    return {
        "no_loss": all_consistent,
        "total_collected_lines": metrics.collected_lines,
        "total_archived_lines_metric": metrics.archived_lines,
        "total_archive_file_lines": total_archive_file_lines,
        "total_candidates": store.count_candidates(),
        "per_source": per_source,
        "sources_circuit_open": [sid for sid, ps in per_source.items() if ps.get("circuit") == "open"],
    }


# ----------------------- 内部辅助 -----------------------


def _initial_state_for(spec: SourceSpec) -> dict[str, Any]:
    if spec.kind == "file":
        return {"offset": 0, "size": 0, "inode": 0}
    if spec.kind == "folder":
        return {"processed": {}}
    if spec.kind == "api":
        return {"cursor": 0}
    return {}


def _spawn_daemon(store: LogOpsStore, python_executable: str) -> subprocess.Popen | None:
    """以独立会话(start_new_session)拉起 daemon 子进程,stdout/stderr 落 daemon.out 日志。

    独立会话 = agent 退出不连带杀 daemon(撑长跑);与 shell 后台进程同款机制。
    """
    log_path = store.root / "daemon.out.log"
    cmd = [
        python_executable,
        "-m",
        "agent_py_agent.agent.tooling.log_ops.daemon",
        str(store.root.parent),  # root 根目录(store.root 已含 monitor_id 子目录)
        "--monitor-id",
        store.monitor_id,
    ]
    repo_root = _repo_root()
    env = dict(os.environ)
    # 保证子进程能 import agent_py_agent(把仓库根放进 PYTHONPATH 头部)。
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    try:
        handle = log_path.open("ab")
    except OSError:
        handle = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        if _IS_WINDOWS:
            process = subprocess.Popen(  # noqa: S603
                cmd, cwd=str(repo_root), stdout=handle, stderr=subprocess.STDOUT, env=env
            )
        else:
            process = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=str(repo_root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
    except OSError:
        return None
    finally:
        if hasattr(handle, "close"):
            handle.close()
    return process


def _repo_root() -> Path:
    # 本文件: <repo>/agent_py_agent/agent/tooling/log_ops/manager.py → 上溯 5 级到 <repo>。
    return Path(__file__).resolve().parents[4]


def _record_pid(record: dict[str, Any]) -> int:
    try:
        return int(record.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> str:
    if pid <= 0:
        return "noop"
    if _IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10)
            return "taskkill/T/F"
        except (OSError, subprocess.SubprocessError):
            return "failed"
    if not _posix_signal(pid, signal.SIGTERM):
        return "already_gone"
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "SIGTERM"
        time.sleep(0.05)
    _posix_signal(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    return "SIGTERM->SIGKILL"


def _posix_signal(pid: int, sig: int) -> bool:
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = pid
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        pass
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


__all__ = [
    "daemon_liveness",
    "reconciliation",
    "start_daemon",
    "stop_daemon",
]
