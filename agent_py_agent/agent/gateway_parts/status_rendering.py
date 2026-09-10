from __future__ import annotations

# LLM: Gateway liveness is the canonical readiness fact for CLI and TUI startup. Waiting uses a
# monotonic bounded deadline so wall-clock jumps cannot extend or shorten the configured budget.
# 模块用途: 提供 Gateway 存活探测和状态展示；交互界面通过这里判断是否可以开始发送请求。

"""Gateway status and liveness rendering helpers."""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..runtime_errors import runtime_error_report
from .daemon_control import (
    get_running_pid,
    get_running_pid_report,
    read_pid_record_report,
)
from .io import gateway_request_counts, read_json_file_report
from .paths import GatewayPaths, gateway_chunk_path

if TYPE_CHECKING:
    from ...core import SimpleAgent


@dataclass(frozen=True)
class GatewayRunningReport:
    pid: int | None
    alive: bool
    load_error: dict | None = None


@dataclass(frozen=True)
class GatewayHeartbeatFacts:
    updated_at: float
    age_seconds: float


@dataclass(frozen=True)
class GatewayStatusRenderContext:
    paths: GatewayPaths
    running_report: GatewayRunningReport
    status: str
    heartbeat_at: float
    heartbeat_age_seconds: float


_GATEWAY_LOG_SAMPLE_MAX_BYTES = 64 * 1024
_EXCEPTION_SIGNATURE_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.]*\.)?([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Interrupt)):\s*"
)


# LLM: 这是 CLI、HTTP 和模型只读工具可共同消费的 Gateway 运行事实投影；健康结论只读
#   PID/state/heartbeat/queue，日志仅作观测诊断，绝不能反向改变运行状态或任务终态。
# 函数用途: 从唯一 Gateway 状态目录生成结构化健康、端点、配置与本生命周期日志摘要。
def gateway_runtime_snapshot(
    agent: SimpleAgent,
    paths: GatewayPaths,
    *,
    include_log_diagnostics: bool = False,
) -> dict[str, object]:
    running = gateway_running_report(paths)
    state_report = read_json_file_report(paths.state, context="gateway.runtime_snapshot.state.read")
    heartbeat_report = read_json_file_report(
        paths.heartbeat,
        context="gateway.runtime_snapshot.heartbeat.read",
    )
    state = state_report.payload
    heartbeat = heartbeat_report.payload
    heartbeat_at = _float_value(heartbeat.get("updated_at"))
    heartbeat_facts = GatewayHeartbeatFacts(
        heartbeat_at,
        max(0.0, time.time() - heartbeat_at) if heartbeat_at else 0.0,
    )
    status = _gateway_status(agent, running, state, heartbeat_facts)
    snapshot = _gateway_runtime_base_snapshot(
        agent,
        paths,
        running=running,
        state=state,
        heartbeat=heartbeat,
        heartbeat_facts=heartbeat_facts,
        status=status,
    )
    pid_record = read_pid_record_report(paths.pid)
    identity = snapshot.get("identity")
    if isinstance(identity, dict):
        identity["process_start_time"] = (
            (pid_record.payload or {}).get("start_time") if pid_record.payload else None
        )
    load_errors = {
        name: report
        for name, report in (
            ("pid", running.load_error or pid_record.load_error),
            ("state", state_report.load_error),
            ("heartbeat", heartbeat_report.load_error),
        )
        if report is not None
    }
    if load_errors:
        snapshot["load_errors"] = load_errors
    if include_log_diagnostics:
        snapshot["log_diagnostics"] = gateway_log_diagnostics(
            paths.log,
            start_offset_bytes=_int_value(state.get("log_start_offset_bytes")),
        )
    return snapshot


# LLM: v2 将进程身份与启动默认模型分开；Gateway 服务多个 owner/会话，启动配置绝不能充当当前调用模型。
# 函数用途: 组装网关健康事实；默认模型只标记为部署信息，不新增 I/O，也不猜当前会话使用哪个模型。
def _gateway_runtime_base_snapshot(
    agent: SimpleAgent,
    paths: GatewayPaths,
    *,
    running: GatewayRunningReport,
    state: dict[str, object],
    heartbeat: dict[str, object],
    heartbeat_facts: GatewayHeartbeatFacts,
    status: str,
) -> dict[str, object]:
    config = getattr(agent, "config", None)
    bind_host = str(
        state.get("http_bind_host")
        or getattr(config, "gateway_bind_host", "127.0.0.1")
        or "127.0.0.1"
    ).strip()
    port = _int_value(state.get("http_port") or getattr(config, "gateway_port", 0))
    started_at = _float_value(state.get("started_at"))
    queue_counts = gateway_request_counts(paths, include_archives=False)
    return {
        "schema": "gateway_runtime_snapshot.v2",
        "identity": {
            "pid": running.pid,
            "process_start_time": None,
            "config_path": str(
                state.get("config_path") or getattr(config, "config_path", "") or ""
            ),
        },
        "deployment_defaults": {
            "model_name": str(state.get("model_name") or ""),
            "scope": "gateway_startup_only",
            "is_current_request_model": False,
        },
        "status": status,
        "alive": running.alive,
        "uptime_seconds": round(max(0.0, time.time() - started_at), 3) if started_at else None,
        "heartbeat": {
            "updated_at": heartbeat_facts.updated_at or None,
            "age_seconds": round(heartbeat_facts.age_seconds, 3),
            "stale_after_seconds": max(
                0,
                int(getattr(config, "gateway_stale_seconds", 0) or 0),
            ),
        },
        "http": {
            "bind_host": bind_host,
            "port": port,
            "base_url": f"http://{bind_host}:{port}" if bind_host and port else "",
            "status_path": "/status",
            "metrics_path": "/metrics",
        },
        "queue": {
            **queue_counts,
            "oldest_pending_age_seconds": _float_value(
                (heartbeat.get("queue_ages") or {}).get("oldest_pending_age_seconds")
                if isinstance(heartbeat.get("queue_ages"), dict)
                else 0
            ),
            "oldest_processing_age_seconds": _float_value(
                (heartbeat.get("queue_ages") or {}).get("oldest_processing_age_seconds")
                if isinstance(heartbeat.get("queue_ages"), dict)
                else 0
            ),
            "inflight": (
                dict(heartbeat.get("inflight") or {})
                if isinstance(heartbeat.get("inflight"), dict)
                else {}
            ),
        },
        "runtime": {
            "gateway_workspace": str(paths.root),
            "log_path": str(paths.log),
        },
        "authority": {
            "process": "validated_gateway_pid_record",
            "health": "gateway_state_and_heartbeat",
            "endpoint": "gateway_process_state_with_config_fallback",
            "log_diagnostics": "observational_only",
        },
    }


# LLM: 日志扫描必须从状态文件记录的本生命周期字节偏移开始，且只返回错误签名计数；
#   不能把原始日志、请求正文、密钥或用户消息复制进模型上下文。
# 函数用途: 有界统计当前 Gateway 生命周期新增日志中的异常类型和噪声规模。
def gateway_log_diagnostics(
    path: Path,
    *,
    start_offset_bytes: int = 0,
    sample_max_bytes: int = _GATEWAY_LOG_SAMPLE_MAX_BYTES,
) -> dict[str, object]:
    offset = max(0, int(start_offset_bytes or 0))
    limit = max(1, int(sample_max_bytes or _GATEWAY_LOG_SAMPLE_MAX_BYTES))
    try:
        size = max(0, int(path.stat().st_size))
        rotated = offset > size
        lifecycle_start = 0 if rotated else offset
        sample_start = max(lifecycle_start, size - limit)
        with path.open("rb") as stream:
            stream.seek(sample_start)
            raw = stream.read(limit)
    except OSError as exc:
        return {
            "status": "unavailable",
            "path": str(path),
            "load_error": runtime_error_report(
                exc,
                context="gateway.runtime_snapshot.log.read",
            ),
        }
    if sample_start > lifecycle_start:
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline >= 0 else b""
    lines = [
        line.strip()
        for line in raw.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    exception_counts: dict[str, int] = {}
    traceback_count = 0
    loop_error_count = 0
    for line in lines:
        if line.startswith("Traceback (most recent call last)"):
            traceback_count += 1
        match = _EXCEPTION_SIGNATURE_RE.match(line)
        if match:
            key = match.group(1)
            exception_counts[key] = exception_counts.get(key, 0) + 1
        if line.startswith("[gateway-loop-error]"):
            loop_error_count += 1
    signature_total = sum(exception_counts.values()) + loop_error_count
    lifecycle_bytes = max(0, size - lifecycle_start)
    if signature_total or traceback_count:
        status = "noise_observed"
    elif lifecycle_bytes == 0:
        status = "no_new_log_bytes"
    else:
        status = "quiet"
    return {
        "status": status,
        "evidence_scope": "current_lifecycle_bounded_file_tail",
        "path": str(path),
        "lifecycle_start_offset_bytes": lifecycle_start,
        "current_size_bytes": size,
        "current_lifecycle_bytes": lifecycle_bytes,
        "sampled_bytes": len(raw),
        "sample_truncated": sample_start > lifecycle_start,
        "rotation_detected": rotated,
        "nonempty_line_count": len(lines),
        "traceback_count": traceback_count,
        "gateway_loop_error_count": loop_error_count,
        "exception_counts": dict(sorted(exception_counts.items())),
        "raw_log_included": False,
    }


def gateway_running(paths: GatewayPaths) -> tuple[int, bool]:
    pid = get_running_pid(paths.pid)
    return pid, bool(pid)


def gateway_running_report(paths: GatewayPaths) -> GatewayRunningReport:
    pid_report = get_running_pid_report(paths.pid)
    return GatewayRunningReport(pid_report.pid, bool(pid_report.pid), pid_report.load_error)


# LLM: This wait must honor the configured upper bound and return the last observed PID. Keep
# readiness polling here as the sole source used by plain CLI and visible TUI preflight.
# 函数用途: 在给定秒数内轮询 Gateway 存活状态，成功立即返回，超时则返回最后观察结果。
def wait_for_gateway_running(paths: GatewayPaths, timeout: float = 3.0) -> tuple[int, bool]:
    deadline = time.monotonic() + max(0.0, timeout)
    last_pid = 0
    while True:
        pid, alive = gateway_running(paths)
        if pid:
            last_pid = pid
        if alive:
            return pid, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return pid or last_pid, False
        time.sleep(min(0.2, remaining))


def render_gateway_status(agent: SimpleAgent, paths: GatewayPaths) -> list[str]:
    running_report = gateway_running_report(paths)
    state_report = read_json_file_report(paths.state, context="gateway.status.state.read")
    heartbeat_report = read_json_file_report(paths.heartbeat, context="gateway.status.heartbeat.read")
    heartbeat = heartbeat_report.payload
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    heartbeat_facts = GatewayHeartbeatFacts(heartbeat_at, time.time() - heartbeat_at if heartbeat_at else 0)
    status = _gateway_status(agent, running_report, state_report.payload, heartbeat_facts)
    lines = _base_status_lines(
        GatewayStatusRenderContext(
            paths,
            running_report,
            status,
            heartbeat_facts.updated_at,
            heartbeat_facts.age_seconds,
        )
    )
    _append_status_load_errors(lines, running_report, state_report.load_error, heartbeat_report.load_error)
    return lines


def _gateway_status(
    agent: SimpleAgent,
    running_report: GatewayRunningReport,
    state: dict,
    heartbeat: GatewayHeartbeatFacts,
) -> str:
    status = "running" if running_report.alive else state.get("status", "stopped")
    if running_report.alive and heartbeat.updated_at and heartbeat.age_seconds > agent.config.gateway_stale_seconds:
        return "stale"
    return status


def _base_status_lines(context: GatewayStatusRenderContext) -> list[str]:
    now = time.time()
    lines = [
        f"gateway status={context.status} "
        f"pid={context.running_report.pid if context.running_report.pid else '-'} "
        f"alive={context.running_report.alive}",
        "gateway requests="
        + json.dumps(gateway_request_counts(context.paths), ensure_ascii=False, sort_keys=True),
    ]
    if context.heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={context.heartbeat_age_seconds:.1f}")
    _append_processing_request_lines(lines, context.paths, now)
    return lines


def _append_processing_request_lines(lines: list[str], paths: GatewayPaths, now: float) -> None:
    active_requests, load_errors, omitted_count = _processing_request_facts(paths, now)
    if active_requests:
        lines.append("gateway active_requests=" + _json_list(active_requests))
    if omitted_count:
        lines.append(f"gateway active_requests_omitted={omitted_count}")
    for load_error in load_errors:
        lines.append("gateway processing_load_error=" + _json(load_error))


def _processing_request_facts(paths: GatewayPaths, now: float) -> tuple[list[dict], list[dict], int]:
    active_requests: list[dict] = []
    load_errors: list[dict] = []
    request_paths = sorted(paths.processing.glob("*.json"))
    for request_path in request_paths[:5]:
        report = read_json_file_report(request_path, context="gateway.status.processing.read")
        if report.load_error:
            load_errors.append(report.load_error)
            continue
        active_requests.append(_processing_request_row(paths, request_path.stem, report.payload, now))
    return active_requests, load_errors, max(0, len(request_paths) - 5)


def _processing_request_row(
    paths: GatewayPaths,
    request_id: str,
    payload: dict,
    now: float,
) -> dict:
    resolved_id = str(payload.get("id") or request_id)
    row = {
        "id": resolved_id,
        "status": str(payload.get("status") or ""),
        "lease_owner": str(payload.get("lease_owner") or ""),
        "attempts": _int_value(payload.get("attempts")),
    }
    _add_age(row, "lease_age_seconds", payload.get("lease_started_at"), now)
    _add_age(row, "lease_heartbeat_age_seconds", payload.get("lease_heartbeat_at"), now)
    _add_age(row, "updated_age_seconds", payload.get("updated_at"), now)
    chunk_path = gateway_chunk_path(paths, resolved_id)
    if chunk_path.exists():
        row["chunk_stream_path"] = str(chunk_path)
    return row


def _add_age(row: dict, key: str, timestamp: object, now: float) -> None:
    timestamp_value = _float_value(timestamp)
    if timestamp_value:
        row[key] = round(max(0.0, now - timestamp_value), 1)


def _float_value(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _append_status_load_errors(
    lines: list[str],
    running_report: GatewayRunningReport,
    state_load_error: dict | None,
    heartbeat_load_error: dict | None,
) -> None:
    if state_load_error:
        lines.append("gateway state_load_error=" + _json(state_load_error))
    if heartbeat_load_error:
        lines.append("gateway heartbeat_load_error=" + _json(heartbeat_load_error))
    if running_report.load_error:
        lines.append("gateway pid_load_error=" + _json(running_report.load_error))


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _json_list(payload: list[dict]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
