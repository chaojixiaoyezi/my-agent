# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""audit logging keeps request/response paths bundled around gateway events.

This module is derived from runtime.py split. It contains all audit-related
logging functions that were previously in that file.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .logging import _report_gateway_side_effect_error, log_gateway_payload

if TYPE_CHECKING:
    from ...core import SimpleAgent


# LLM: AuditRequestCompletedParams 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存audit请求completed参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class AuditRequestCompletedParams:
    response: dict
    request: dict
    request_path: Path
    response_path: Path


# LLM: audit_request_processing 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理audit请求processing相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def audit_request_processing(
    agent: SimpleAgent,
    context: dict,
) -> None:
    request = context["request"]
    log_gateway_payload(
        agent,
        {
            **request,
            "id": context["request_id"],
            "kind": context["kind"] or "unknown",
            "status": "processing",
            "ok": False,
            "started_at": context["started_at"],
        },
        event_type="gateway_request_processing",
        request_path=context["request_path"],
        response_path=context["response_path"],
    )


# LLM: audit_request_completed 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理audit请求completed相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def audit_request_completed(
    agent: SimpleAgent,
    *,
    params: AuditRequestCompletedParams,
) -> None:
    response = params.response
    event_type = (
        "gateway_request_completed" if response.get("ok") else "gateway_request_failed"
    )
    log_gateway_payload(
        agent,
        {**response, "prompt": params.request.get("prompt", "")},
        event_type=event_type,
        request_path=params.request_path,
        response_path=params.response_path,
    )


# LLM: audit_request_queued 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理audit请求queued相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def audit_request_queued(
    agent: SimpleAgent,
    payload: dict,
    request_path: Path,
    response_path: Path,
) -> None:
    log_gateway_payload(
        agent,
        {**payload, "status": "queued", "ok": False},
        event_type="gateway_request_queued",
        request_path=request_path,
        response_path=response_path,
    )


# LLM: audit_heartbeat_abandoned 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理auditheartbeatabandoned相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def audit_heartbeat_abandoned(
    agent: SimpleAgent,
    request_id: str,
    failures: int,
    request_path: Path,
) -> None:
    log_gateway_payload(
        agent,
        {"id": request_id, "status": "heartbeat_abandoned", "failures": failures},
        event_type="gateway_heartbeat_abandoned",
        request_path=request_path,
    )


# LLM: audit_side_effect_error 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理auditsideeffecterror相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def audit_side_effect_error(
    operation: str,
    request_id: str,
    exc: Exception,
) -> None:
    _report_gateway_side_effect_error(operation, request_id, exc)
