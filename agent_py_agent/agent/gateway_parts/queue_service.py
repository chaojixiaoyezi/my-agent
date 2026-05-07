# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""Queue iteration and state management for gateway processing.

This module is derived from runtime.py split. It contains queue scanning,
file state transitions, and lease management that were previously in that file.
"""

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .daemon_control import get_running_pid
from .io import (
    append_gateway_history,
    gateway_request_counts,
    gateway_response_path,
    read_json_file,
    write_json_file,
)

# Re-export heartbeat liveness check for backward compatibility
from .lease_service import is_heartbeat_alive_for_request
from .logging import GatewayIndexPayloadOptions, _index_gateway_payload
from .paths import GatewayPaths, gateway_paths
from .recovery import _archive_gateway_request, _gateway_request_attempts

if TYPE_CHECKING:
    from ...core import SimpleAgent

_CLAIM_LOCK = threading.Lock()


# LLM: gateway_running 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关running相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def gateway_running(paths: GatewayPaths) -> tuple[int, bool]:
    pid = get_running_pid(paths.pid)
    return pid, bool(pid)


# LLM: wait_for_gateway_running 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关running的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def wait_for_gateway_running(paths: GatewayPaths, timeout: float = 10.0) -> tuple[int, bool]:
    deadline = time.time() + max(0.0, timeout)
    last_pid = 0
    while True:
        pid, alive = gateway_running(paths)
        if pid:
            last_pid = pid
        if alive:
            return pid, True
        if time.time() >= deadline:
            return pid or last_pid, False
        time.sleep(0.2)


# LLM: render_gateway_status 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 渲染或汇总网关状态的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def render_gateway_status(agent: SimpleAgent, paths: GatewayPaths) -> list[str]:
    pid, alive = gateway_running(paths)
    state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    age = time.time() - heartbeat_at if heartbeat_at else 0
    stale = bool(heartbeat_at and age > agent.config.gateway_stale_seconds)
    status = "running" if alive else state.get("status", "stopped")
    if alive and stale:
        status = "stale"

    lines = [
        f"gateway status={status} pid={pid if pid else '-'} alive={alive}",
        "gateway requests=" + json.dumps(gateway_request_counts(paths), ensure_ascii=False, sort_keys=True),
    ]
    if heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={age:.1f}")
    return lines


# LLM: rebuild_gateway_index 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理rebuild网关index相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def rebuild_gateway_index(agent: SimpleAgent) -> int:
    paths = gateway_paths(agent)
    return (
        _rebuild_gateway_history_index(agent, paths)
        + _rebuild_gateway_request_file_index(agent, paths)
        + _rebuild_gateway_response_index(agent, paths)
    )


# LLM: _rebuild_gateway_history_index 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理rebuild网关historyindex相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _rebuild_gateway_history_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    count = 0
    if not paths.history.exists():
        return count
    for line in paths.history.read_text(encoding="utf-8", errors="replace").splitlines():
        payload = _payload_from_history_line(line)
        if not payload:
            continue
        response_path = gateway_response_path(paths, str(payload.get("id") or ""))
        if _index_gateway_payload(agent, payload, GatewayIndexPayloadOptions(response_path=response_path)):
            count += 1
    return count


# LLM: _payload_from_history_line 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理来自载荷historyline相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _payload_from_history_line(line: str) -> dict:
    if not line.strip():
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _rebuild_gateway_request_file_index 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理rebuild网关请求文件index相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _rebuild_gateway_request_file_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    count = 0
    for request_path in _iter_gateway_request_files(paths):
        if _index_gateway_request_file(agent, paths, request_path):
            count += 1
    return count


# LLM: _iter_gateway_request_files 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理迭代网关请求文件相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _iter_gateway_request_files(paths: GatewayPaths):
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed):
        yield from sorted(folder.glob("*.json"))


# LLM: _index_gateway_request_file 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理index网关请求文件相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _index_gateway_request_file(agent: SimpleAgent, paths: GatewayPaths, request_path: Path) -> bool:
    payload = read_json_file(request_path)
    if not payload:
        return False
    request_id = str(payload.get("id") or request_path.stem)
    response_path = gateway_response_path(paths, request_id)
    response_payload = read_json_file(response_path)
    merged = {**payload, **response_payload} if response_payload else payload
    return _index_gateway_payload(
        agent,
        merged,
        GatewayIndexPayloadOptions(request_path=request_path, response_path=response_path),
    )


# LLM: _rebuild_gateway_response_index 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理rebuild网关响应index相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _rebuild_gateway_response_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    count = 0
    for response_path in sorted(paths.responses.glob("*.json")):
        payload = read_json_file(response_path)
        if not payload:
            continue
        if _index_gateway_payload(agent, payload, GatewayIndexPayloadOptions(response_path=response_path)):
            count += 1
    return count


# LLM: ensure_gateway_folders 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 校验网关folders需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def ensure_gateway_folders(paths: GatewayPaths) -> None:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.done.mkdir(parents=True, exist_ok=True)
    paths.failed.mkdir(parents=True, exist_ok=True)
    paths.responses.mkdir(parents=True, exist_ok=True)


# LLM: claim_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理claim请求相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def claim_request(paths: GatewayPaths, request_path: Path) -> Path | None:
    from .logging import _report_gateway_side_effect_error

    processing_path = paths.processing / request_path.name
    with _CLAIM_LOCK:
        if not request_path.exists() or processing_path.exists():
            return None
        try:
            request_path.replace(processing_path)
            return processing_path
        except OSError as exc:
            _report_gateway_side_effect_error("claim_gateway_request", request_path.stem, exc)
            return None


# LLM: archive_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入archive请求的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def archive_request(processing_path: Path, target_folder: Path, request_id: str) -> bool:
    from .logging import _report_gateway_side_effect_error

    try:
        _archive_gateway_request(processing_path, target_folder)
        return True
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_request", request_id, exc)
        return False


# LLM: materialize_missing_archive 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理materializemissingarchive相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def materialize_missing_archive(target_folder: Path, request_id: str, response: dict) -> Path:
    target_folder.mkdir(parents=True, exist_ok=True)
    target = target_folder / f"{request_id}.json"
    if target.exists():
        return target
    payload = {
        "id": request_id,
        "status": response.get("status", "done" if response.get("ok") else "failed"),
        "ok": bool(response.get("ok")),
        "attempts": response.get("attempts", 0),
        "lease_owner": response.get("lease_owner", ""),
        "lease_started_at": response.get("lease_started_at", 0),
        "lease_heartbeat_at": response.get("lease_heartbeat_at", 0),
        "completed_at": response.get("ended_at", time.time()),
        "archive_note": "request file was already moved or removed before final archive",
    }
    write_json_file(target, payload)
    return target
