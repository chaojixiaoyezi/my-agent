# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""Request execution and handling for gateway."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .audit_service import audit_request_queued
from .io import (
    append_gateway_history,
    gateway_response_path,
    read_json_file,
    write_json_file,
)
from .paths import GatewayPaths
from .queue_service import (
    archive_request,
    claim_request,
    ensure_gateway_folders,
    materialize_missing_archive,
)
from .recovery import _gateway_request_attempts
from .request_execution import _handle_gateway_request

if TYPE_CHECKING:
    from ...core import SimpleAgent


# LLM: GatewayAskParams 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关ask参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class GatewayAskParams:
    prompt: str
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool = True
    include_prompt: bool = False
    client_wait: bool = True
    context_scope: str = "default"
    resume_context: bool | None = None
    task_attributes: dict | None = None
    agent: SimpleAgent | None = field(default=None, repr=False)


# LLM: _ClaimedGatewayRequestContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存claimed网关请求上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _ClaimedGatewayRequestContext:

    agent: SimpleAgent
    processing_path: Path
    request_payload: dict
    request_id: str
    worker_id: str


# LLM: submit_gateway_ask 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送网关ask请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    params: GatewayAskParams,
) -> tuple[str, Path, Path]:
    from .io import write_gateway_request

    request_id_obj = str(time.time() * 1000)[:13]
    request_id = f"gw-{request_id_obj}"

    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": params.prompt,
        "inject": params.inject or [],
        "prompt_files": params.prompt_files or [],
        "save": params.save,
        "include_prompt": params.include_prompt,
        "client_wait": params.client_wait,
        "context_scope": params.context_scope,
        "task_attributes": dict(params.task_attributes or {}),
        "created_at": time.time(),
        "client_pid": 0,
        "status": "pending",
        "attempts": 0,
    }
    if params.resume_context is not None:
        payload["resume_context"] = bool(params.resume_context)
    response_path = gateway_response_path(paths, request_id)
    _attach_runtime_task_card(payload, params=params, response_path=response_path)
    request_path = write_gateway_request(paths, payload)
    if params.agent is not None:
        audit_request_queued(
            params.agent,
            {**payload, "status": "queued", "ok": False},
            request_path,
            response_path,
        )
    return request_id, request_path, response_path


# LLM: _attach_runtime_task_card projects no-wait gateway requests into durable TaskCards at enqueue time.
# 函数用途: 为后台 gateway 请求创建可恢复任务卡，并把 task_id 写回请求机器字段。
def _attach_runtime_task_card(
    payload: dict,
    *,
    params: GatewayAskParams,
    response_path: Path,
) -> None:
    if params.agent is None or params.client_wait:
        return
    cards_root = getattr(params.agent, "runtime_cards_root", None)
    messages_root = getattr(params.agent, "runtime_messages_root", None)
    if cards_root is None or messages_root is None:
        return
    from ..cards import CardStore, SessionCard
    from ..messages import MessageStore, MessageTool
    from ..runtime import TaskRuntime

    cards = CardStore(cards_root)
    user_id = str(getattr(getattr(params.agent, "config", None), "user_id", "") or "admin")
    session_id = f"gateway-{user_id}"
    try:
        cards.get_session(session_id)
    except KeyError:
        cards.save_session(SessionCard(session_id=session_id, user_id=user_id, channel="gateway"))
    runtime = TaskRuntime(cards, MessageTool(MessageStore(messages_root)))
    attrs = dict(params.task_attributes or {})
    task = runtime.create_task(
        goal=params.prompt,
        user_id=user_id,
        session_id=session_id,
        complexity="complex",
        acceptance=[],
    )
    task.metadata.update(
        {
            "gateway_request_id": payload["id"],
            "gateway_response_path": str(response_path),
            **attrs,
        }
    )
    cards.save_task(task)
    cards.attach_task_to_session(session_id, task.task_id)
    payload["runtime_task_id"] = task.task_id
    payload["runtime_session_id"] = session_id


# LLM: wait_for_gateway_response 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关响应的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    path = gateway_response_path(paths, request_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_json_file(path)
        if payload:
            return payload
        time.sleep(0.2)
    return {}


# LLM: _process_gateway_requests 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关requests的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _process_gateway_requests(agent: SimpleAgent, paths: GatewayPaths, *, worker_id: str = "gw-worker") -> int:
    ensure_gateway_folders(paths)
    processed = 0
    for request_path in _pending_request_paths_for_worker(agent, paths, worker_id):
        if _process_gateway_request_path(agent, paths, request_path, worker_id):
            processed += 1
    return processed


# LLM: _pending_request_paths_for_worker keeps one gateway worker reserved for interactive asks.
# 函数用途: 多 worker 时让 gw-worker-0 只消费前台请求，避免 no-wait 长任务占满用户聊天通道。
def _pending_request_paths_for_worker(agent: SimpleAgent, paths: GatewayPaths, worker_id: str) -> list[Path]:
    request_paths = sorted(paths.inbox.glob("*.json"), key=lambda path: (_request_lane_sort_key(path), path.name))
    if not _worker_reserved_for_foreground(agent, worker_id):
        return request_paths
    return [path for path in request_paths if _request_is_foreground(path)]


# LLM: _worker_reserved_for_foreground derives reservation from the stable worker id and config.
# 函数用途: 只有多 worker 网关才保留 0 号前台 worker；单 worker 仍兼容处理全部请求。
def _worker_reserved_for_foreground(agent: SimpleAgent, worker_id: str) -> bool:
    try:
        worker_count = int(getattr(agent.config, "gateway_request_workers", 1) or 1)
    except (TypeError, ValueError):
        worker_count = 1
    try:
        reserved_count = int(getattr(agent.config, "gateway_foreground_reserved_workers", 1) or 0)
    except (TypeError, ValueError):
        reserved_count = 1
    index = _worker_index(worker_id)
    return worker_count > 1 and reserved_count > 0 and index is not None and index < min(worker_count, reserved_count)


# LLM: _worker_index parses stable gateway worker ids without adding queue metadata.
# 函数用途: 从 gw-worker-N 取出 N，用于前台保留 worker 判断；解析失败时不保留。
def _worker_index(worker_id: str) -> int | None:
    suffix = str(worker_id or "").strip().rsplit("-", 1)[-1]
    try:
        return int(suffix)
    except ValueError:
        return None


# LLM: _request_lane_sort_key prioritizes synchronous client waits without changing queue format.
# 函数用途: 其它 worker 也优先处理前台请求；没有 client_wait 的旧请求按前台兼容。
def _request_lane_sort_key(path: Path) -> int:
    return 0 if _request_is_foreground(path) else 1


# LLM: _request_is_foreground reads a machine flag, not prompt wording.
# 函数用途: client_wait=false 表示 ask --no-wait 后台长任务；缺省为 True 兼容旧请求。
def _request_is_foreground(path: Path) -> bool:
    payload = read_json_file(path) or {}
    return bool(payload.get("client_wait", True))


# LLM: _process_gateway_request_path 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求路径的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _process_gateway_request_path(
    agent: SimpleAgent,
    paths: GatewayPaths,
    request_path: Path,
    worker_id: str,
) -> bool:
    processing_path = claim_request(paths, request_path)
    if processing_path is None:
        return False
    request_payload = read_json_file(processing_path)
    request_id = str(request_payload.get("id") or processing_path.stem)
    request_payload.setdefault("id", request_id)
    if gateway_response_path(paths, request_id).exists():
        archive_request(processing_path, paths.done, request_id)
        return True
    response = _process_claimed_gateway_request(
        _ClaimedGatewayRequestContext(agent, processing_path, request_payload, request_id, worker_id)
    )
    _finish_claimed_gateway_request(paths, processing_path, request_id, response)
    return True


# LLM: _process_claimed_gateway_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进claimed网关请求的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _process_claimed_gateway_request(context: _ClaimedGatewayRequestContext) -> dict:
    from .io import write_json_file_atomic
    from .logging import _report_gateway_side_effect_error

    _mark_request_processing(context.request_payload, context.worker_id)
    try:
        write_json_file_atomic(context.processing_path, context.request_payload)
    except OSError as exc:
        _report_gateway_side_effect_error("prepare_gateway_request_lease", context.request_id, exc)
        _write_processing_payload_fallback(context.processing_path, context.request_payload, context.request_id)
        return _handle_gateway_request(
            context.agent,
            context.processing_path,
            refresh_lease=False,
            worker_id=context.worker_id,
        )
    return _handle_gateway_request(
        context.agent,
        context.processing_path,
        refresh_lease=True,
        worker_id=context.worker_id,
    )


# LLM: _mark_request_processing 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 更新processing对应的任务或运行状态，并保留既有字段语义；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _mark_request_processing(request_payload: dict, worker_id: str) -> None:
    lease_now = time.time()
    request_payload.update(
        {
            "status": "processing",
            "attempts": _gateway_request_attempts(request_payload) + 1,
            "lease_owner": worker_id,
            "lease_started_at": lease_now,
            "lease_heartbeat_at": lease_now,
            "updated_at": lease_now,
        }
    )


# LLM: _write_processing_payload_fallback 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入processing载荷fallback的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _write_processing_payload_fallback(processing_path: Path, request_payload: dict, request_id: str) -> None:
    from .logging import _report_gateway_side_effect_error

    try:
        write_json_file(processing_path, request_payload)
    except OSError as fallback_exc:
        _report_gateway_side_effect_error("prepare_gateway_request_lease_fallback", request_id, fallback_exc)


# LLM: _finish_claimed_gateway_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理finishclaimed网关请求相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _finish_claimed_gateway_request(
    paths: GatewayPaths,
    processing_path: Path,
    request_id: str,
    response: dict,
) -> None:
    response_path = gateway_response_path(paths, str(response.get("id", processing_path.stem)))
    if not response_path.exists():
        write_json_file(response_path, response)
    append_gateway_history(paths, response)
    target_folder = paths.done if response.get("ok") else paths.failed
    _write_final_request_archive_payload(processing_path, response)
    archived = archive_request(processing_path, target_folder, request_id)
    if not archived and not processing_path.exists():
        materialize_missing_archive(target_folder, request_id, response)


# LLM: _write_final_request_archive_payload 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入final请求archive载荷的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _write_final_request_archive_payload(processing_path: Path, response: dict) -> None:
    request_payload = read_json_file(processing_path)
    if not request_payload:
        return
    request_payload.update(
        {
            "status": str(request_payload.get("status") or response.get("status") or ""),
            "attempts": response.get("attempts", request_payload.get("attempts", 0)),
            "lease_owner": response.get("lease_owner", request_payload.get("lease_owner", "")),
            "lease_started_at": response.get("lease_started_at", request_payload.get("lease_started_at", 0)),
            "lease_heartbeat_at": response.get("lease_heartbeat_at", request_payload.get("lease_heartbeat_at", 0)),
            "completed_at": response.get("ended_at", time.time()),
        }
    )
    write_json_file(processing_path, request_payload)
