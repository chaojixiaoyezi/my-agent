from __future__ import annotations

"""Authoritative execution lifecycle for side-effecting tools.

The runtime gate decides whether a call may run.  This coordinator decides
whether this exact operation may run *now*.  Its durable claim is written
before the tool implementation is entered; audit ledgers remain best-effort
and are deliberately not consulted for execution authority.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..local_storage import (
    TOOL_OPERATION_FAILED,
    TOOL_OPERATION_SUCCEEDED,
    ToolOperationClaim,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationRecord,
    new_tool_operation_holder,
)
from .models import ToolExecutionResult

_RESULT_SCHEMA = "tool_execution_result.v1"
_MINIMUM_LEASE_SECONDS = 900
_LEASE_GRACE_SECONDS = 300


@dataclass(frozen=True)
class ToolOperationExecutionRequest:
    store: object | None
    store_required: bool
    owner_id: str
    run_id: str
    task_id: str
    operation_id: str
    tool_name: str
    args_hash: str
    idempotency_key: str
    idempotency_scope: str
    idempotency_namespace: str
    timeout_seconds: int
    invoke: Callable[[], ToolExecutionResult]


def execute_tool_operation(
    request: ToolOperationExecutionRequest,
) -> ToolExecutionResult:
    """Claim, execute once, persist, and replay an exact side-effect result."""

    if not _operation_store_available(request.store):
        if request.store_required:
            result = _operation_error(
                request.tool_name,
                "TOOL_OPERATION_STORE_UNAVAILABLE",
                "副作用操作账本当前不可用，系统已在执行前安全停止。",
            )
            _attach_operation_facts(
                result,
                request,
                status="not_started",
                action="store_unavailable",
                diagnostic="missing_operation_store_contract",
            )
            return result
        return request.invoke()
    claim = _claim_operation(request)
    if isinstance(claim, ToolExecutionResult):
        return claim
    existing = _existing_claim_result(request, claim)
    if existing is not None:
        return existing
    return _execute_claimed_operation(request, claim)


def _operation_store_available(store: object | None) -> bool:
    return bool(
        store is not None
        and hasattr(store, "claim_tool_operation")
        and hasattr(store, "finish_tool_operation")
    )


def _claim_operation(
    request: ToolOperationExecutionRequest,
) -> ToolOperationClaim | ToolExecutionResult:
    holder = new_tool_operation_holder()
    lease_seconds = max(
        _MINIMUM_LEASE_SECONDS,
        max(0, int(request.timeout_seconds or 0)) + _LEASE_GRACE_SECONDS,
    )
    try:
        claim = request.store.claim_tool_operation(
            ToolOperationClaimRequest(
                owner_id=request.owner_id,
                run_id=request.run_id,
                task_id=request.task_id,
                operation_id=request.operation_id,
                tool=request.tool_name,
                args_hash=request.args_hash,
                idempotency_key=request.idempotency_key,
                idempotency_scope=request.idempotency_scope,
                idempotency_namespace=request.idempotency_namespace,
                holder=holder,
                lease_expires_at=time.time() + lease_seconds,
            )
        )
    except Exception as exc:  # noqa: BLE001 - authoritative store failures fail closed
        result = _operation_error(
            request.tool_name,
            "TOOL_OPERATION_STORE_UNAVAILABLE",
            "副作用操作账本无法建立执行占位，工具没有运行。",
        )
        _attach_operation_facts(
            result,
            request,
            status="not_started",
            action="store_unavailable",
            diagnostic=type(exc).__name__,
        )
        return result
    return claim


def _existing_claim_result(
    request: ToolOperationExecutionRequest,
    claim: ToolOperationClaim,
) -> ToolExecutionResult | None:
    if claim.action == "replay":
        result = _result_from_record(claim.record)
        if result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN":
            _attach_operation_facts(
                result,
                request,
                status="unknown",
                action="reconcile",
                diagnostic="terminal_result_unreadable",
            )
            return result
        delivery = result.result_envelope.get("delivery_evidence")
        if isinstance(delivery, dict):
            delivery["deduplicated"] = True
        _attach_operation_facts(
            result,
            request,
            status=claim.record.status,
            action="replay",
            replayed=True,
        )
        return result
    if claim.action == "execute":
        return None
    code, message, action = _claim_block_contract(claim.action)
    result = _operation_error(request.tool_name, code, message)
    _attach_operation_facts(
        result,
        request,
        status=claim.record.status,
        action=action,
        diagnostic=claim.reason if claim.reason else claim.action,
    )
    return result


def _claim_block_contract(action: str) -> tuple[str, str, str]:
    contracts = {
        "in_flight": (
            "TOOL_OPERATION_IN_FLIGHT",
            "同一次副作用操作仍在执行，系统没有启动第二份。",
            "wait",
        ),
        "unknown": (
            "TOOL_OPERATION_OUTCOME_UNKNOWN",
            "此前执行可能已经产生副作用，但没有保存终态；为防止重复，系统不会自动重做。",
            "reconcile",
        ),
        "conflict": (
            "TOOL_OPERATION_IDENTITY_CONFLICT",
            "同一操作身份被用于不同的结构化调用，系统已拒绝执行。",
            "repair_identity",
        ),
    }
    return contracts.get(
        action,
        (
            "TOOL_OPERATION_STORE_UNAVAILABLE",
            "副作用操作账本返回了无法识别的执行状态，工具没有运行。",
            "invalid_claim",
        ),
    )


def _execute_claimed_operation(
    request: ToolOperationExecutionRequest,
    claim: ToolOperationClaim,
) -> ToolExecutionResult:
    result = request.invoke()
    terminal_status = (
        TOOL_OPERATION_SUCCEEDED if result.ok else TOOL_OPERATION_FAILED
    )
    try:
        request.store.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id=request.owner_id,
                run_id=request.run_id,
                operation_id=request.operation_id,
                holder_id=claim.record.holder_id,
                generation=claim.record.generation,
                status=terminal_status,
                result=_result_payload(result),
                error_code=result.error_code,
            )
        )
    except Exception as exc:  # noqa: BLE001 - effect already happened; never invite retry
        _attach_operation_facts(
            result,
            request,
            status="unknown",
            action="completion_persistence_failed",
            diagnostic=type(exc).__name__,
        )
        return result
    _attach_operation_facts(
        result,
        request,
        status=terminal_status,
        action="executed",
    )
    return result


def _result_payload(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "schema_version": _RESULT_SCHEMA,
        "tool": result.tool,
        "ok": result.ok,
        "output": result.output,
        "call_id": result.call_id,
        "result_envelope": dict(result.result_envelope or {}),
        "error_code": result.error_code,
        "reported_error_code": result.reported_error_code,
        "error_category": result.error_category,
        "retryable": result.retryable,
        "recommended_action": result.recommended_action,
        "recovery_hint": result.recovery_hint,
    }


def _result_from_record(record: ToolOperationRecord) -> ToolExecutionResult:
    payload = record.result if isinstance(record.result, dict) else {}
    if payload.get("schema_version") != _RESULT_SCHEMA:
        return _operation_error(
            record.tool,
            "TOOL_OPERATION_OUTCOME_UNKNOWN",
            "已有副作用操作缺少可重放的完整结果，系统不会重复执行。",
        )
    result = ToolExecutionResult(
        tool=str(payload.get("tool") or record.tool),
        ok=payload.get("ok") is True,
        output=str(payload.get("output") or ""),
        call_id=str(payload.get("call_id") or ""),
        result_envelope=(
            dict(payload.get("result_envelope") or {})
            if isinstance(payload.get("result_envelope"), dict)
            else {}
        ),
        error_code=str(payload.get("error_code") or ""),
        reported_error_code=str(payload.get("reported_error_code") or ""),
    )
    if not result.ok:
        result.error_category = str(
            payload.get("error_category") or result.error_category
        )
        result.retryable = bool(payload.get("retryable", result.retryable))
        result.recommended_action = str(
            payload.get("recommended_action") or result.recommended_action
        )
        result.recovery_hint = str(
            payload.get("recovery_hint") or result.recovery_hint
        )
    return result


def _operation_error(
    tool_name: str,
    error_code: str,
    message: str,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name,
        False,
        json.dumps(
            {"ok": False, "error": message, "error_code": error_code},
            ensure_ascii=False,
        ),
        error_code=error_code,
    )


def _attach_operation_facts(
    result: ToolExecutionResult,
    request: ToolOperationExecutionRequest,
    *,
    status: str,
    action: str,
    replayed: bool = False,
    diagnostic: str = "",
) -> None:
    facts: dict[str, object] = {
        "schema_version": "tool_operation.v1",
        "operation_id": request.operation_id,
        "status": status,
        "action": action,
        "replayed": replayed,
        "idempotency_scope": request.idempotency_scope,
    }
    if diagnostic:
        facts["diagnostic"] = str(diagnostic)[:240]
    result.result_envelope.setdefault("tool_operation", {}).update(facts)


__all__ = [
    "ToolOperationExecutionRequest",
    "execute_tool_operation",
]
