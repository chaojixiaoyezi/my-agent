# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""execution helpers keep one claimed gateway request inside focused contexts."""

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent_core.runtime_mixin import RunParams
from .audit_service import (
    AuditRequestCompletedParams,
    audit_request_completed,
    audit_request_processing,
)
from .chunk_service import close_chunk_stream, open_chunk_stream, write_chunk
from .io import gateway_response_path, read_json_file
from .lease_service import refresh_processing_lease, start_lease_heartbeat
from .paths import gateway_chunk_path, gateway_paths
from .recovery import _gateway_request_attempts

if TYPE_CHECKING:
    from ...core import SimpleAgent

_EMPTY_PROMPT_MESSAGE = "gateway ask prompt/goal cannot be empty"


# LLM: _GatewayResponseBaseContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关响应基础上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _GatewayResponseBaseContext:

    request: dict
    request_path: Path
    request_id: str
    kind: str
    started_at: float


# LLM: _GatewayAskRunContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关askrun上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _GatewayAskRunContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    response_path: Path
    request_id: str
    on_chunk: object


# LLM: _GatewayLeaseStartContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关租约start上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _GatewayLeaseStartContext:

    agent: SimpleAgent
    request: dict
    request_path: Path
    request_id: str
    refresh_lease: bool
    worker_id: str


# LLM: _build_gateway_response_base 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 构建网关响应基础所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_gateway_response_base(context: _GatewayResponseBaseContext) -> dict:
    request = context.request
    return {
        "id": context.request_id,
        "kind": context.kind or "unknown",
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": context.started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "",
        "error": "",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(context.request_path),
        "attempts": _gateway_request_attempts(request),
        "lease_owner": request.get("lease_owner", ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }


# LLM: _update_response_from_result 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 更新来自响应结果对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新请求队列、租约文件、进程状态和响应渲染，需避免破坏既有状态机约定。
def _update_response_from_result(response: dict, result, request: dict) -> None:
    response.update(
        {
            "ok": True,
            "status": "done",
            "response": result.response,
            "backend": result.backend,
            "used_memories": result.used_memories,
            "tool_rounds": result.tool_rounds,
            "prompt": result.prompt if request.get("include_prompt") else "",
            "prompt_token_estimate": result.prompt_token_estimate,
            "runtime_injection_token_estimate": result.runtime_injection_token_estimate,
            "recovery_snapshot_id": result.recovery_snapshot_id,
            "recovery_snapshot_path": result.recovery_snapshot_path,
            "recovery_snapshot_error": result.recovery_snapshot_error,
            "memory_resume_context_injected": result.memory_resume_context_injected,
            "memory_resume_context_query": result.memory_resume_context_query,
            "memory_resume_context_matches": result.memory_resume_context_matches,
            "memory_resume_context_token_estimate": result.memory_resume_context_token_estimate,
            "memory_resume_context_error": result.memory_resume_context_error,
        }
    )


# LLM: _start_gateway_request_lease 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求租约的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _start_gateway_request_lease(
    context: _GatewayLeaseStartContext,
) -> tuple[threading.Event | None, threading.Thread | None]:
    should_refresh = context.refresh_lease or str(context.request.get("status") or "") == "processing"
    if not should_refresh:
        return None, None
    lease_worker = context.worker_id or str(context.request.get("lease_owner") or "")
    refresh_processing_lease(context.request_path, request_id=context.request_id, worker_id=lease_worker)
    return start_lease_heartbeat(context.agent, context.request_path, request_id=context.request_id, worker_id=lease_worker)


# LLM: _run_gateway_ask 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关ask的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _run_gateway_ask(context: _GatewayAskRunContext):
    request = context.request
    prompt = str(request.get("prompt") or request.get("goal") or "").strip()
    if not prompt:
        raise ValueError(_EMPTY_PROMPT_MESSAGE)
    return context.agent.run(
        prompt,
        params=RunParams(
            inject=[str(item) for item in request.get("inject", [])],
            prompt_files=[str(item) for item in request.get("prompt_files", [])],
            save=bool(request.get("save", True)),
            request_id=context.request_id,
            source="gateway",
            recovery_snapshot=bool(request.get("save", True)),
            resume_context=request.get("resume_context") if "resume_context" in request else None,
            recovery_next_actions=[
                "If this gateway request must be recovered, inspect the gateway response and LocalStore gateway_request records first."
            ],
            recovery_content_paths=[str(context.request_path), str(context.response_path)],
            on_chunk=context.on_chunk,
        ),
    )


# LLM: _stop_gateway_request_lease 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求租约的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _stop_gateway_request_lease(
    lease_stop: threading.Event | None,
    lease_thread: threading.Thread | None,
) -> None:
    if lease_stop is not None:
        lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=2)


# LLM: _copy_final_lease_fields 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理copyfinal租约字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _copy_final_lease_fields(response: dict, request_path: Path) -> None:
    final_request = read_json_file(request_path)
    if not final_request:
        return
    response["lease_owner"] = final_request.get("lease_owner", response.get("lease_owner", ""))
    response["lease_started_at"] = final_request.get("lease_started_at", response.get("lease_started_at", 0))
    response["lease_heartbeat_at"] = final_request.get(
        "lease_heartbeat_at",
        response.get("lease_heartbeat_at", 0),
    )


# LLM: _execute_gateway_request_body 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求body的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _execute_gateway_request_body(context: dict, on_chunk) -> None:
    response = context["response"]
    kind = str(response.get("kind") or "").strip()
    if kind != "ask":
        response["error_code"] = "UNSUPPORTED_KIND"
        raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
    try:
        result = _run_gateway_ask(
            _GatewayAskRunContext(
                context["agent"],
                context["request"],
                context["request_path"],
                context["response_path"],
                context["request_id"],
                on_chunk,
            )
        )
    except ValueError as exc:
        if str(exc) == _EMPTY_PROMPT_MESSAGE:
            response["error_code"] = "EMPTY_PROMPT"
        raise
    _update_response_from_result(response, result, context["request"])


# LLM: _prepare_gateway_request_context 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理prepare网关请求上下文相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _prepare_gateway_request_context(agent: SimpleAgent, request_path: Path) -> dict:
    request = read_json_file(request_path)
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip() or ("ask" if request_id else "")
    response_path = gateway_response_path(gateway_paths(agent), request_id)
    existing_response = read_json_file(response_path)
    if existing_response:
        return {"existing_response": existing_response}
    started_at = time.time()
    response = _build_gateway_response_base(
        _GatewayResponseBaseContext(request, request_path, request_id, kind, started_at)
    )
    context = {
        "request": request,
        "request_id": request_id,
        "kind": kind,
        "started_at": started_at,
        "request_path": request_path,
        "response_path": response_path,
        "response": response,
    }
    audit_request_processing(agent, context)
    return context


# LLM: _finalize_gateway_response 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理finalize网关响应相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _finalize_gateway_response(context: dict, response: dict) -> None:
    ended_at = time.time()
    _copy_final_lease_fields(response, context["request_path"])
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - context["started_at"], 3)


# LLM: _complete_gateway_request_audit 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理complete网关请求audit相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _complete_gateway_request_audit(agent: SimpleAgent, context: dict, request_path: Path, response: dict) -> None:
    audit_request_completed(
        agent,
        params=AuditRequestCompletedParams(
            response=response,
            request=context["request"],
            request_path=request_path,
            response_path=context["response_path"],
        ),
    )


# LLM: _handle_gateway_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进网关请求的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    context = _prepare_gateway_request_context(agent, request_path)
    if context.get("existing_response"):
        return context["existing_response"]
    response = context["response"]
    lease_stop, lease_thread = _start_gateway_request_lease(
        _GatewayLeaseStartContext(
            agent,
            context["request"],
            request_path,
            context["request_id"],
            refresh_lease,
            worker_id,
        )
    )
    chunk_path = gateway_chunk_path(gateway_paths(agent), context["request_id"])
    chunk_path_abs, _ = open_chunk_stream(chunk_path)

    try:
        _execute_gateway_request_body({**context, "agent": agent}, lambda chunk: write_chunk(chunk_path_abs, chunk))
    except Exception as exc:
        response.update(
            {
                "ok": False,
                "status": "failed",
                "error_code": response.get("error_code") or type(exc).__name__.upper(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        _stop_gateway_request_lease(lease_stop, lease_thread)
        close_chunk_stream(chunk_path_abs)
    _finalize_gateway_response(context, response)
    _complete_gateway_request_audit(agent, context, request_path, response)
    return response
