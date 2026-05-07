from __future__ import annotations

"""LLM: mirrors gateway lifecycle and request payloads into LocalStore with non-fatal errors.

给人看的解释：
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


@dataclass(frozen=True)
class _GatewayPayloadRenderContext:

    payload: dict
    request_id: str
    kind: str
    status: str
    request_path: Path | None
    response_path: Path | None


@dataclass(frozen=True)
class GatewayIndexPayloadOptions:
    request_path: Path | None = None
    response_path: Path | None = None
    event_type: str = "gateway_request_rebuilt"


def log_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    *,
    event_type: str,
    request_path: Path | None = None,
    response_path: Path | None = None,
) -> None:

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
                _GatewayPayloadRenderContext(payload, request_id, kind, status, request_path, response_path)
            ),
            metadata=_gateway_payload_metadata(
                _GatewayPayloadRenderContext(payload, request_id, kind, status, request_path, response_path)
            ),
            event_type=event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_payload", request_id, exc)


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


def _report_gateway_side_effect_error(operation: str, request_id: str, exc: Exception) -> None:
    try:
        print(
            f"[gateway-side-effect-error] operation={operation} request_id={request_id} "
            f"error_type={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
    except Exception:
        pass
