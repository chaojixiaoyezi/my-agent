# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""detects, requeues, fails, and archives stale gateway processing requests.

gateway 如果崩在半路，请求会留在 processing 目录。
这个文件专门处理这种'卡住的请求'：能重试就退回 pending，重试太多就写失败响应并归档。
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .io import append_gateway_history, gateway_response_path, read_json_file, write_json_file
from .logging import _report_gateway_side_effect_error, log_gateway_payload
from .paths import GatewayPaths

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: _RecoveryContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存恢复上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _RecoveryContext:
    now: float
    max_attempts: int
    timeout_seconds: int
    startup: bool
    agent: SimpleAgent | None
    lease_stale_seconds: int | None


# LLM: gateway_stale_processing 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关staleprocessing相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def gateway_stale_processing(paths: GatewayPaths, timeout_seconds: int) -> list[dict]:

    items: list[dict] = []
    now = time.time()
    timeout_seconds = max(1, int(timeout_seconds or 1))
    for path in sorted(paths.processing.glob("*.json")):
        payload = read_json_file(path)
        request_id = str(payload.get("id") or path.stem)
        if gateway_response_path(paths, request_id).exists():
            continue
        lease_at = _gateway_processing_lease_at(payload, path)
        age = now - lease_at if lease_at else 0
        if lease_at and age < timeout_seconds:
            continue
        items.append(
            {
                "request_id": request_id,
                "path": str(path),
                "age_seconds": round(age, 1) if lease_at else 0,
                "attempts": _gateway_request_attempts(payload),
                "lease_owner": str(payload.get("lease_owner") or ""),
                "lease_heartbeat_at": payload.get("lease_heartbeat_at", 0),
                "lease_started_at": payload.get("lease_started_at", 0),
            }
        )
    return items


# LLM: recover_gateway_processing_requests 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理recover网关processingrequests相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def recover_gateway_processing_requests(
    paths: GatewayPaths,
    *,
    params: _RecoveryContext | None = None,
    max_attempts: int = 2,
    timeout_seconds: int = 900,
    startup: bool = False,
    agent: SimpleAgent | None = None,
    lease_stale_seconds: int | None = None,
) -> dict[str, int]:

    _ensure_recovery_dirs(paths)
    summary = {"requeued": 0, "failed": 0, "checked": 0, "archived": 0}
    now = time.time()
    max_attempts = _non_negative_int(max_attempts, default=1)
    timeout_seconds = max(1, int(timeout_seconds or 1))
    context = params or _RecoveryContext(now, max_attempts, timeout_seconds, startup, agent, lease_stale_seconds)
    for request_path in sorted(paths.processing.glob("*.json")):
        summary["checked"] += 1
        action = _recover_one_processing_request(paths, request_path, context)
        if action in summary:
            summary[action] += 1
    return summary


# LLM: _ensure_recovery_dirs 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 校验恢复dirs需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _ensure_recovery_dirs(paths: GatewayPaths) -> None:
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)


# LLM: _recover_one_processing_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理recoveroneprocessing请求相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _recover_one_processing_request(
    paths: GatewayPaths,
    request_path: Path,
    context: _RecoveryContext,
) -> str:
    payload = read_json_file(request_path) or {"id": request_path.stem, "kind": "unknown", "created_at": 0}
    request_id = str(payload.get("id") or request_path.stem)
    if gateway_response_path(paths, request_id).exists():
        return _archive_completed_processing(paths, request_path, request_id)
    if not _processing_request_stale(payload, request_path, context):
        return ""
    attempts = _gateway_request_attempts(payload)
    if context.max_attempts > 0 and attempts >= context.max_attempts:
        return _fail_stale_processing(
            {
                "paths": paths,
                "request_path": request_path,
                "payload": payload,
                "timeout_seconds": context.timeout_seconds,
                "attempts": attempts,
                "agent": context.agent,
            }
        )
    return _requeue_stale_processing(paths, request_path, payload, context)


def _non_negative_int(value: object, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


# LLM: _archive_completed_processing 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入completedprocessing的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _archive_completed_processing(paths: GatewayPaths, request_path: Path, request_id: str) -> str:
    try:
        _archive_gateway_request(request_path, paths.done)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_duplicate_gateway_processing_request", request_id, exc)
        return ""
    return "archived"


# LLM: _processing_request_stale 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进processing请求stale的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _processing_request_stale(
    payload: dict,
    request_path: Path,
    context: _RecoveryContext,
) -> bool:
    from .runtime import is_heartbeat_alive_for_request

    request_id = str(payload.get("id") or request_path.stem)
    lease_at = _gateway_processing_lease_at(payload, request_path)
    effective_timeout = context.lease_stale_seconds if context.lease_stale_seconds is not None else context.timeout_seconds
    return (
        context.startup
        or not lease_at
        or (context.now - lease_at >= effective_timeout and not is_heartbeat_alive_for_request(request_id))
    )


# LLM: _fail_stale_processing 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理failstaleprocessing相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _fail_stale_processing(context: dict) -> str:
    paths = context["paths"]
    request_path = context["request_path"]
    _write_gateway_failure_response(
        paths,
        {
            "request_path": request_path,
            "payload": context["payload"],
            "status": "failed",
            "error_code": "GATEWAY_PROCESSING_TIMEOUT",
            "error": f"gateway processing timeout after {context['timeout_seconds']}s; attempts={context['attempts']}",
            "event_type": "gateway_request_processing_failed",
            "agent": context["agent"],
        },
    )
    try:
        _archive_gateway_request(request_path, paths.failed)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_failed_request", request_path.stem, exc)
        return ""
    return "failed"


# LLM: _requeue_stale_processing 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理requeuestaleprocessing相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _requeue_stale_processing(
    paths: GatewayPaths,
    request_path: Path,
    payload: dict,
    context: _RecoveryContext,
) -> str:
    payload.update(
        {
            "status": "pending",
            "requeued_at": context.now,
            "last_error": (
                "gateway restarted before request completed"
                if context.startup
                else f"gateway processing timeout after {context.timeout_seconds}s"
            ),
        }
    )
    try:
        write_json_file(request_path, payload)
        request_path.replace(paths.inbox / request_path.name)
    except OSError as exc:
        _report_gateway_side_effect_error("requeue_gateway_request", request_path.stem, exc)
        return ""
    return "requeued"


# LLM: requeue_gateway_processing_requests 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理requeue网关processingrequests相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def requeue_gateway_processing_requests(paths: GatewayPaths) -> int:

    return recover_gateway_processing_requests(paths, startup=True)["requeued"]


# LLM: _gateway_request_attempts 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关请求attempts相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _gateway_request_attempts(payload: dict) -> int:

    try:
        return int(payload.get("attempts", 0) or 0)
    except (TypeError, ValueError):
        return 0


# LLM: _gateway_processing_started_at 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关processingstartedat相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _gateway_processing_started_at(payload: dict, request_path: Path) -> float:

    return _gateway_processing_timestamp(
        payload,
        request_path,
        ("lease_started_at", "started_at", "updated_at", "created_at"),
    )


# LLM: _gateway_processing_lease_at 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关processing租约at相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _gateway_processing_lease_at(payload: dict, request_path: Path) -> float:

    return _gateway_processing_timestamp(
        payload,
        request_path,
        ("lease_heartbeat_at", "lease_started_at", "started_at", "updated_at", "created_at"),
    )


# LLM: _gateway_processing_timestamp 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关processingtimestamp相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _gateway_processing_timestamp(payload: dict, request_path: Path, keys: tuple[str, ...]) -> float:
    for key in keys:
        try:
            value = float(payload.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    try:
        return request_path.stat().st_mtime
    except OSError:
        return 0


# LLM: _write_gateway_failure_response 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入网关失败响应的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _write_gateway_failure_response(
    paths: GatewayPaths,
    context: dict,
) -> dict:

    request_path = context["request_path"]
    payload = context["payload"]
    request_id = str(payload.get("id") or request_path.stem)
    now = time.time()
    started_at = _gateway_processing_started_at(payload, request_path) or now
    response = {
        "id": request_id,
        "kind": str(payload.get("kind") or "unknown"),
        "ok": False,
        "status": context["status"],
        "created_at": payload.get("created_at", 0),
        "started_at": started_at,
        "ended_at": now,
        "duration_seconds": round(now - started_at, 3),
        "response": "",
        "error_code": context["error_code"],
        "error": context["error"],
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": _gateway_request_attempts(payload),
    }
    response_path = gateway_response_path(paths, request_id)
    if not response_path.exists():
        write_json_file(response_path, response)
    append_gateway_history(paths, response)
    agent = context.get("agent")
    if agent:
        log_gateway_payload(
            agent,
            {**response, "prompt": payload.get("prompt", "")},
            event_type=context["event_type"],
            request_path=request_path,
            response_path=response_path,
        )
    return response


# LLM: _archive_gateway_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入网关请求的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _archive_gateway_request(path: Path, target_dir: Path) -> Path:

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
    path.replace(target)
    return target
