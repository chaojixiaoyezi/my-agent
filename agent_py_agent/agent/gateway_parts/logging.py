# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""mirrors gateway lifecycle and request payloads into LocalStore with non-fatal errors.

文件队列是 gateway 的事实源，LocalStore 是方便搜索和排查的索引。
这个文件负责把请求、响应、生命周期事件写进索引；索引失败会报告，但不会弄坏主请求。
"""

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: _GatewayPayloadRenderContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关载荷render上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _GatewayPayloadRenderContext:

    payload: dict
    request_id: str
    kind: str
    status: str
    request_path: Path | None
    response_path: Path | None


# LLM: GatewayIndexPayloadOptions 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关index载荷选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class GatewayIndexPayloadOptions:
    request_path: Path | None = None
    response_path: Path | None = None
    event_type: str = "gateway_request_rebuilt"


# LLM: GatewayPayloadLogParams 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关载荷log参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class GatewayPayloadLogParams:
    event_type: str
    request_path: Path | None = None
    response_path: Path | None = None


# LLM: log_gateway_payload 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入网关载荷的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def log_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    *,
    params: GatewayPayloadLogParams | None = None,
    event_type: str,
    request_path: Path | None = None,
    response_path: Path | None = None,
) -> None:
    log_params = params or GatewayPayloadLogParams(event_type, request_path, response_path)
    request_id = str(payload.get("id") or "")
    if not request_id:
        return
    try:
        status = str(payload.get("status") or "queued")
        kind = str(payload.get("kind") or "unknown")
        agent.local_store.log_record(
            source_type="gateway_request",
            source_id=request_id,
            title=f"Gateway {kind} {status} {request_id}",
            content=_gateway_payload_content(
                _GatewayPayloadRenderContext(
                    payload, request_id, kind, status, log_params.request_path, log_params.response_path
                )
            ),
            metadata=_gateway_payload_metadata(
                _GatewayPayloadRenderContext(
                    payload, request_id, kind, status, log_params.request_path, log_params.response_path
                )
            ),
            event_type=log_params.event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_payload", request_id, exc)


# LLM: _gateway_payload_content 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关载荷内容相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _gateway_payload_content(context: _GatewayPayloadRenderContext) -> str:
    payload = context.payload
    return "\n".join(
        [
            "# Gateway Request",
            f"id: {context.request_id}",
            f"kind: {context.kind}",
            f"status: {context.status}",
            f"ok: {payload.get('ok', '')}",
            f"backend: {payload.get('backend', '')}",
            f"tool_rounds: {payload.get('tool_rounds', '')}",
            f"prompt: {payload.get('prompt', '')}",
            f"response: {payload.get('response', '')}",
            f"error_code: {payload.get('error_code', '')}",
            f"error: {payload.get('error', '')}",
            f"request_file: {context.request_path or payload.get('request_file', '')}",
            f"response_file: {context.response_path or ''}",
            "",
            "## Payload",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )


# LLM: _gateway_payload_metadata 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理网关载荷metadata相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _gateway_payload_metadata(context: _GatewayPayloadRenderContext) -> dict:
    payload = context.payload
    return {
        "request_id": context.request_id,
        "kind": context.kind,
        "status": context.status,
        "ok": bool(payload.get("ok", False)),
        "backend": str(payload.get("backend", "")),
        "tool_rounds": int(payload.get("tool_rounds", 0) or 0),
        "error_code": str(payload.get("error_code", "")),
        "created_at": float(payload.get("created_at", 0) or 0),
        "started_at": float(payload.get("started_at", 0) or 0),
        "ended_at": float(payload.get("ended_at", 0) or 0),
        "request_path": str(context.request_path or payload.get("request_file", "")),
        "response_path": str(context.response_path or ""),
    }


# LLM: log_gateway_event 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入网关event的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def log_gateway_event(agent: SimpleAgent, event_type: str, payload: dict) -> None:

    try:
        created_at = time.time()
        source_id = f"{event_type}:{created_at:.6f}"
        agent.local_store.log_record(
            source_type="gateway_event",
            source_id=source_id,
            title=f"Gateway event {event_type}",
            content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            metadata={
                "event_type": event_type,
                "status": str(payload.get("status", "")),
                "pid": int(payload.get("pid", 0) or 0),
                "created_at": created_at,
            },
            event_type=event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_event", str(payload.get("id", event_type)), exc)


# LLM: _index_gateway_payload 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理index网关载荷相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _index_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    options: GatewayIndexPayloadOptions | None = None,
) -> bool:

    options = options or GatewayIndexPayloadOptions()
    request_path = options.request_path
    response_path = options.response_path
    event_type = options.event_type
    request_id = str(payload.get("id") or (request_path.stem if request_path else "")).strip()
    if not request_id:
        return False
    log_gateway_payload(
        agent,
        {
            **payload,
            "id": request_id,
            "kind": str(payload.get("kind") or "ask"),
            "status": str(payload.get("status") or "rebuilt"),
            "ok": bool(payload.get("ok", False)),
        },
        event_type=event_type,
        request_path=request_path,
        response_path=response_path,
    )
    return True


# LLM: _report_gateway_side_effect_error 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理报告网关sideeffecterror相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _report_gateway_side_effect_error(operation: str, request_id: str, exc: Exception) -> None:
    try:
        print(
            f"[gateway-side-effect-error] operation={operation} request_id={request_id} "
            f"error_type={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
    except Exception:
        pass
