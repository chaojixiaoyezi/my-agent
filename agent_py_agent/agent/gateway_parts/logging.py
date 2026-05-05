from __future__ import annotations

"""LLM: mirrors gateway lifecycle and request payloads into LocalStore with non-fatal errors.

给人看的解释：
文件队列是 gateway 的事实源，LocalStore 是方便搜索和排查的索引。
这个文件负责把请求、响应、生命周期事件写进索引；索引失败会报告，但不会弄坏主请求。
"""

import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


def log_gateway_payload(
    agent: SimpleAgent,
    payload: dict,
    *,
    event_type: str,
    request_path: Path | None = None,
    response_path: Path | None = None,
) -> None:
    """LLM contract: index a gateway request/response payload into LocalStore.

    Human version:
    文件队列是事实源，LocalStore 是搜索索引。这里负责把 gateway 的关键请求和响应同步进索引；
    如果索引失败，会把错误写到 stderr，但不会让 gateway 请求本身失败。
    """

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
            content=_gateway_payload_content(payload, request_id, kind, status, request_path, response_path),
            metadata=_gateway_payload_metadata(payload, request_id, kind, status, request_path, response_path),
            event_type=event_type,
        )
    except Exception as exc:
        _report_gateway_side_effect_error("log_gateway_payload", request_id, exc)


def _gateway_payload_content(
    payload: dict,
    request_id: str,
    kind: str,
    status: str,
    request_path: Path | None,
    response_path: Path | None,
) -> str:
    return "\n".join(
        [
            "# Gateway Request",
            f"id: {request_id}",
            f"kind: {kind}",
            f"status: {status}",
            f"ok: {payload.get('ok', '')}",
            f"backend: {payload.get('backend', '')}",
            f"tool_rounds: {payload.get('tool_rounds', '')}",
            f"prompt: {payload.get('prompt', '')}",
            f"response: {payload.get('response', '')}",
            f"error_code: {payload.get('error_code', '')}",
            f"error: {payload.get('error', '')}",
            f"request_file: {request_path or payload.get('request_file', '')}",
            f"response_file: {response_path or ''}",
            "",
            "## Payload",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        ]
    )


def _gateway_payload_metadata(
    payload: dict,
    request_id: str,
    kind: str,
    status: str,
    request_path: Path | None,
    response_path: Path | None,
) -> dict:
    return {
        "request_id": request_id,
        "kind": kind,
        "status": status,
        "ok": bool(payload.get("ok", False)),
        "backend": str(payload.get("backend", "")),
        "tool_rounds": int(payload.get("tool_rounds", 0) or 0),
        "error_code": str(payload.get("error_code", "")),
        "created_at": float(payload.get("created_at", 0) or 0),
        "started_at": float(payload.get("started_at", 0) or 0),
        "ended_at": float(payload.get("ended_at", 0) or 0),
        "request_path": str(request_path or payload.get("request_file", "")),
        "response_path": str(response_path or ""),
    }


def log_gateway_event(agent: SimpleAgent, event_type: str, payload: dict) -> None:
    """LLM contract: index a gateway lifecycle event into LocalStore.

    Human version:
    start/stop/restart/heartbeat 这类事件不是用户请求，但排查后台状态时很有用，所以也写入 LocalStore。
    """

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
    *,
    request_path: Path | None = None,
    response_path: Path | None = None,
    event_type: str = "gateway_request_rebuilt",
) -> bool:
    """LLM contract: index one gateway payload during LocalStore rebuild.

    Human version:
    重建索引时会从很多文件里读 payload。只要能识别 request_id，就复用 gateway 日志索引逻辑写入 LocalStore。
    """

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
    """LLM contract: report non-fatal gateway side-effect failures with context.

    Human version:
    有些失败不该打断主请求，比如写索引失败、归档失败。但也不能悄悄吃掉，所以统一把
    operation、request_id、错误类型和错误内容写到 stderr。
    """
    try:
        print(
            f"[gateway-side-effect-error] operation={operation} request_id={request_id} "
            f"error_type={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
    except Exception:
        pass
