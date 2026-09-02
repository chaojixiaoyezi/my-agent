"""Audit logging keeps request/response paths bundled around gateway events."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .logging import _report_gateway_side_effect_error, log_gateway_payload

if TYPE_CHECKING:
    from ...core import SimpleAgent


@dataclass(frozen=True)
class AuditRequestCompletedParams:
    response: dict
    request: dict
    request_path: Path
    response_path: Path


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


# LLM: Completion replaces the same LocalStore source row written at queued/processing time, so
# host-authored conversation_runtime must be carried forward or historical task paths disappear.
# 函数用途: 将 Gateway 最终响应、原始请求和精确会话任务引用一起写入历史索引。
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
        {
            **response,
            "prompt": params.request.get("prompt", ""),
            "conversation_runtime": params.request.get("conversation_runtime", {}),
        },
        event_type=event_type,
        request_path=params.request_path,
        response_path=params.response_path,
    )


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


def audit_side_effect_error(
    operation: str,
    request_id: str,
    exc: Exception,
) -> None:
    _report_gateway_side_effect_error(operation, request_id, exc)
