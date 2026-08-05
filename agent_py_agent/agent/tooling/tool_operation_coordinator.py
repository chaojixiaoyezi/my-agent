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
    TOOL_OPERATION_UNKNOWN,
    ToolOperationClaim,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationHolder,
    ToolOperationRecord,
    ToolOperationReopenRequest,
    new_tool_operation_holder,
)
from .models import (
    ToolExecutionResult,
    ToolFailureStage,
    ToolOperationReconciliation,
    apply_tool_execution_facts,
)

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
    reconcile: Callable[[ToolOperationRecord], ToolOperationReconciliation] | None = None


@dataclass(frozen=True)
class _ToolOperationClaimAttempt:
    claim: ToolOperationClaim
    holder: ToolOperationHolder
    lease_expires_at: float


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
            return apply_tool_execution_facts(
                result,
                failure_stage=ToolFailureStage.PERSISTENCE,
                handler_executed=False,
            )
        return request.invoke()
    attempt = _claim_operation(request)
    if isinstance(attempt, ToolExecutionResult):
        return attempt
    resolved = _resolve_claim(request, attempt)
    if isinstance(resolved, ToolExecutionResult):
        return resolved
    return _execute_claimed_operation(request, resolved)


def _operation_store_available(store: object | None) -> bool:
    return bool(
        store is not None
        and hasattr(store, "claim_tool_operation")
        and hasattr(store, "finish_tool_operation")
    )


def _claim_operation(
    request: ToolOperationExecutionRequest,
) -> _ToolOperationClaimAttempt | ToolExecutionResult:
    holder = new_tool_operation_holder()
    lease_seconds = max(
        _MINIMUM_LEASE_SECONDS,
        max(0, int(request.timeout_seconds or 0)) + _LEASE_GRACE_SECONDS,
    )
    lease_expires_at = time.time() + lease_seconds
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
                lease_expires_at=lease_expires_at,
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
        return apply_tool_execution_facts(
            result,
            failure_stage=ToolFailureStage.PERSISTENCE,
            handler_executed=False,
        )
    return _ToolOperationClaimAttempt(
        claim=claim,
        holder=holder,
        lease_expires_at=lease_expires_at,
    )


def _resolve_claim(
    request: ToolOperationExecutionRequest,
    attempt: _ToolOperationClaimAttempt,
) -> ToolOperationClaim | ToolExecutionResult:
    claim = attempt.claim
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
            return apply_tool_execution_facts(
                result,
                failure_stage=ToolFailureStage.EFFECT_RECONCILIATION,
                handler_executed=False,
            )
        delivery = result.result_envelope.get("delivery_evidence")
        if isinstance(delivery, dict):
            delivery["deduplicated"] = True
        original_execution = _tool_execution_facts(result)
        _attach_operation_facts(
            result,
            request,
            status=claim.record.status,
            action="replay",
            replayed=True,
        )
        result.result_envelope["tool_operation"][
            "original_tool_execution"
        ] = original_execution
        apply_tool_execution_facts(result, handler_executed=False)
        if not result.ok:
            apply_tool_execution_facts(
                result,
                failure_stage=ToolFailureStage.EFFECT_RECONCILIATION,
            )
        return result
    if claim.action == "execute":
        return claim
    if claim.action == "unknown":
        return _reconcile_unknown_operation(request, attempt)
    code, message, action = _claim_block_contract(claim.action)
    result = _operation_error(request.tool_name, code, message)
    _attach_operation_facts(
        result,
        request,
        status=claim.record.status,
        action=action,
        diagnostic=claim.reason if claim.reason else claim.action,
    )
    return apply_tool_execution_facts(
        result,
        failure_stage=ToolFailureStage.EFFECT_RECONCILIATION,
        handler_executed=False,
    )


def _reconcile_unknown_operation(
    request: ToolOperationExecutionRequest,
    attempt: _ToolOperationClaimAttempt,
) -> ToolOperationClaim | ToolExecutionResult:
    claim = attempt.claim
    if request.reconcile is None:
        return _unknown_claim_result(request, claim, diagnostic=claim.reason or "no_reconciler")
    try:
        reconciliation = request.reconcile(claim.record)
    except Exception as exc:  # noqa: BLE001 - reconciliation is read-only and must fail closed
        return _unknown_claim_result(
            request,
            claim,
            diagnostic=f"reconciler_failed:{type(exc).__name__}",
        )
    outcome = str(reconciliation.outcome or "").strip().lower()
    source_ref = str(reconciliation.source_ref or "").strip()
    if outcome not in {
        "succeeded",
        "failed",
        "not_started",
        "safe_to_retry",
        "unknown",
    }:
        return _unknown_claim_result(
            request,
            claim,
            diagnostic=f"invalid_reconciliation_outcome:{outcome or 'empty'}",
        )
    if outcome == "unknown":
        return _unknown_claim_result(
            request,
            claim,
            diagnostic=str(reconciliation.reason or claim.reason or "still_unknown"),
        )
    if not source_ref:
        return _unknown_claim_result(
            request,
            claim,
            diagnostic="reconciliation_source_ref_missing",
        )
    if outcome in {"not_started", "safe_to_retry"}:
        return _reopen_unknown_operation(
            request,
            attempt,
            source_ref=source_ref,
            reconciliation_outcome=outcome,
        )
    reconciled_result = reconciliation.result
    if reconciled_result is None or reconciled_result.ok != (outcome == "succeeded"):
        return _unknown_claim_result(
            request,
            claim,
            diagnostic="reconciliation_result_mismatch",
        )
    terminal_status = (
        TOOL_OPERATION_SUCCEEDED if reconciled_result.ok else TOOL_OPERATION_FAILED
    )
    reconciled_result.effect_source_ref = (
        reconciled_result.effect_source_ref or source_ref
    )
    apply_tool_execution_facts(reconciled_result, handler_executed=False)
    if not reconciled_result.ok:
        apply_tool_execution_facts(
            reconciled_result,
            failure_stage=ToolFailureStage.EFFECT_RECONCILIATION,
        )
    _attach_operation_facts(
        reconciled_result,
        request,
        status=terminal_status,
        action="reconciled",
        replayed=True,
        source_ref=source_ref,
    )
    try:
        request.store.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id=claim.record.owner_id,
                run_id=claim.record.run_id,
                operation_id=claim.record.operation_id,
                holder_id=claim.record.holder_id,
                generation=claim.record.generation,
                status=terminal_status,
                result=_result_payload(reconciled_result),
                error_code=reconciled_result.error_code,
            )
        )
    except Exception as exc:  # noqa: BLE001 - unknown remains authoritative on any store race
        return _unknown_claim_result(
            request,
            claim,
            diagnostic=f"reconciliation_persistence_failed:{type(exc).__name__}",
            source_ref=source_ref,
        )
    return reconciled_result


def _reopen_unknown_operation(
    request: ToolOperationExecutionRequest,
    attempt: _ToolOperationClaimAttempt,
    *,
    source_ref: str,
    reconciliation_outcome: str,
) -> ToolOperationClaim | ToolExecutionResult:
    record = attempt.claim.record
    reopen = getattr(request.store, "reopen_tool_operation_after_reconciliation", None)
    if not callable(reopen):
        return _unknown_claim_result(
            request,
            attempt.claim,
            diagnostic="operation_store_reopen_contract_missing",
        )
    reconciliation_result = _reopened_operation_result(
        request,
        source_ref=source_ref,
        reconciliation_outcome=reconciliation_outcome,
    )
    try:
        reopened = reopen(
            ToolOperationReopenRequest(
                owner_id=record.owner_id,
                run_id=record.run_id,
                operation_id=record.operation_id,
                expected_generation=record.generation,
                holder=attempt.holder,
                lease_expires_at=attempt.lease_expires_at,
                source_ref=source_ref,
                reconciliation_result=_result_payload(reconciliation_result),
            )
        )
    except Exception as exc:  # noqa: BLE001 - a competing reconciliation must win atomically
        return _unknown_claim_result(
            request,
            attempt.claim,
            diagnostic=f"operation_reopen_failed:{type(exc).__name__}",
        )
    return ToolOperationClaim("execute", reopened, source_ref)


def _reopened_operation_result(
    request: ToolOperationExecutionRequest,
    *,
    source_ref: str,
    reconciliation_outcome: str,
) -> ToolExecutionResult:
    retry_safe = reconciliation_outcome == "safe_to_retry"
    result = ToolExecutionResult(
        request.tool_name,
        False,
        json.dumps(
            {
                "ok": False,
                "error": (
                    "目标系统声明同一幂等键可安全重放；同一操作已原子重开。"
                    if retry_safe
                    else "目标系统已证明此前操作未开始；同一操作已原子重开。"
                ),
                "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN",
            },
            ensure_ascii=False,
        ),
        error_code="TOOL_OPERATION_OUTCOME_UNKNOWN",
        effect_outcome="unknown",
        effect_source_ref=source_ref,
        failure_stage=ToolFailureStage.EFFECT_RECONCILIATION.value,
    )
    _attach_operation_facts(
        result,
        request,
        status="running",
        action=(
            "reopened_after_safe_retry_reconciliation"
            if retry_safe
            else "reopened_after_reconciliation"
        ),
        source_ref=source_ref,
    )
    return result


def _unknown_claim_result(
    request: ToolOperationExecutionRequest,
    claim: ToolOperationClaim,
    *,
    diagnostic: str,
    source_ref: str = "",
) -> ToolExecutionResult:
    prior = _result_from_record(claim.record)
    reported = (
        prior.reported_error_code
        if prior.error_code != "TOOL_OPERATION_OUTCOME_UNKNOWN"
        else claim.record.error_code
    )
    result = _operation_error(
        request.tool_name,
        "TOOL_OPERATION_OUTCOME_UNKNOWN",
        "此前执行可能已经产生副作用，但目标系统尚未给出可证明的终态；系统不会自动重做。",
        reported_error_code=reported,
    )
    result.result_envelope["reported_tool_result"] = _reported_tool_result_facts(prior)
    _attach_operation_facts(
        result,
        request,
        status="unknown",
        action="reconcile",
        diagnostic=diagnostic,
        source_ref=source_ref or prior.effect_source_ref,
    )
    return apply_tool_execution_facts(
        result,
        failure_stage=ToolFailureStage.EFFECT_RECONCILIATION,
        handler_executed=False,
    )


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


# LLM: invoke 后的提供方结果只有在权威 operation store 持久化终态后才可作为成功事实。
# 函数用途: 执行已占位的副作用工具、保存终态；保存失败时返回未知且不允许自动重做。
def _execute_claimed_operation(
    request: ToolOperationExecutionRequest,
    claim: ToolOperationClaim,
) -> ToolExecutionResult:
    result = request.invoke()
    operation_status = _operation_status_for_result(result)
    unknown_reason = ""
    if operation_status == TOOL_OPERATION_UNKNOWN:
        result = _unknown_outcome_result(request, result)
        unknown_reason = (
            f"effect_outcome_unknown:{result.reported_error_code or result.error_code}"
        )
    action = (
        "executed_after_reconciliation"
        if claim.reason
        else ("reconcile" if operation_status == TOOL_OPERATION_UNKNOWN else "executed")
    )
    result.effect_source_ref = result.effect_source_ref or claim.reason
    _attach_operation_facts(
        result,
        request,
        status=operation_status,
        action=action,
        source_ref=claim.reason,
    )
    try:
        request.store.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id=claim.record.owner_id,
                run_id=claim.record.run_id,
                operation_id=claim.record.operation_id,
                holder_id=claim.record.holder_id,
                generation=claim.record.generation,
                status=operation_status,
                result=_result_payload(result),
                error_code=result.error_code,
                unknown_reason=unknown_reason,
            )
        )
    except Exception as exc:  # noqa: BLE001 - effect already happened; never invite retry
        # LLM: 工具返回值只是提供方报告；权威账本没有保存终态时，成功与否都必须降级为 unknown，
        # 否则模型会把“提供方说成功”误当成可恢复、可收口的机器事实。
        # 函数用途: 返回不可自动重试的未知结果，同时只保留原结果的安全结构化旁证。
        result = _completion_persistence_unknown_result(
            request,
            result,
        )
        _attach_operation_facts(
            result,
            request,
            status="unknown",
            action="completion_persistence_failed",
            diagnostic=type(exc).__name__,
        )
        return result
    return result


# LLM: 权威终态写入失败后绝不能继续返回 ok=true；原工具报告只作为旁证，不能恢复执行权。
# 函数用途: 把已调用工具的结果转换为不可自动重试的 unknown，并保留最小安全报告字段。
def _completion_persistence_unknown_result(
    request: ToolOperationExecutionRequest,
    reported: ToolExecutionResult,
) -> ToolExecutionResult:
    envelope = dict(reported.result_envelope or {})
    envelope["reported_tool_result"] = _reported_tool_result_facts(reported)
    return ToolExecutionResult(
        tool=request.tool_name,
        ok=False,
        output=json.dumps(
            {
                "ok": False,
                "error": (
                    "工具已经返回，但权威副作用账本未能保存终态；"
                    "操作可能已经生效，系统已阻止自动重做。"
                ),
                "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN",
                "reported_ok": reported.ok,
            },
            ensure_ascii=False,
        ),
        call_id=reported.call_id,
        result_envelope=envelope,
        error_code="TOOL_OPERATION_OUTCOME_UNKNOWN",
        reported_error_code=reported.reported_error_code or reported.error_code,
        effect_outcome="unknown",
        effect_source_ref=reported.effect_source_ref,
        handler_executed=reported.handler_executed,
        failure_stage=ToolFailureStage.PERSISTENCE.value,
        duration_ms=reported.duration_ms,
    )


def _operation_status_for_result(result: ToolExecutionResult) -> str:
    if result.ok:
        return TOOL_OPERATION_SUCCEEDED
    if result.effect_outcome == "not_started":
        return TOOL_OPERATION_FAILED
    if (
        result.effect_outcome == "unknown"
        or result.error_code in {"TOOL_TIMEOUT", "TOOL_OPERATION_OUTCOME_UNKNOWN"}
    ):
        return TOOL_OPERATION_UNKNOWN
    return TOOL_OPERATION_FAILED


def _unknown_outcome_result(
    request: ToolOperationExecutionRequest,
    reported: ToolExecutionResult,
) -> ToolExecutionResult:
    reported_code = (
        reported.reported_error_code
        or reported.error_code
        or "UNKNOWN_ERROR"
    )
    envelope = dict(reported.result_envelope or {})
    envelope["reported_tool_result"] = _reported_tool_result_facts(reported)
    return ToolExecutionResult(
        tool=request.tool_name,
        ok=False,
        output=json.dumps(
            {
                "ok": False,
                "error": "工具调用已结束等待，但副作用是否完成仍不确定；系统已阻止自动重做。",
                "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN",
                "reported_error_code": reported_code,
            },
            ensure_ascii=False,
        ),
        call_id=reported.call_id,
        result_envelope=envelope,
        error_code="TOOL_OPERATION_OUTCOME_UNKNOWN",
        reported_error_code=reported_code,
        effect_outcome="unknown",
        effect_source_ref=reported.effect_source_ref,
        handler_executed=reported.handler_executed,
        failure_stage=ToolFailureStage.EFFECT_RECONCILIATION.value,
        duration_ms=reported.duration_ms,
    )


# LLM: reported_tool_result 只保存类型化旁证，不复制可能含密钥或大正文的工具 output。
# 函数用途: 为 timeout 与账本终态写入失败生成同一份安全、可归档的原始结果摘要。
def _reported_tool_result_facts(
    reported: ToolExecutionResult,
) -> dict[str, object]:
    return {
        "ok": reported.ok,
        "error_code": reported.error_code,
        "reported_error_code": reported.reported_error_code,
        "effect_outcome": reported.effect_outcome,
        "effect_source_ref": reported.effect_source_ref,
        "handler_executed": reported.handler_executed,
        "failure_stage": reported.failure_stage,
        "duration_ms": reported.duration_ms,
    }


def _tool_execution_facts(result: ToolExecutionResult) -> dict[str, object]:
    facts: dict[str, object] = {
        "handler_executed": result.handler_executed,
        "duration_ms": result.duration_ms,
    }
    if result.failure_stage:
        facts["failure_stage"] = result.failure_stage
    return facts


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
        "effect_outcome": result.effect_outcome,
        "effect_source_ref": result.effect_source_ref,
        "handler_executed": result.handler_executed,
        "failure_stage": result.failure_stage,
        "duration_ms": result.duration_ms,
    }


def _result_from_record(record: ToolOperationRecord) -> ToolExecutionResult:
    payload = record.result if isinstance(record.result, dict) else {}
    if payload.get("schema_version") != _RESULT_SCHEMA:
        return _operation_error(
            record.tool,
            "TOOL_OPERATION_OUTCOME_UNKNOWN",
            "已有副作用操作缺少可重放的完整结果，系统不会重复执行。",
        )
    try:
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
            effect_outcome=str(payload.get("effect_outcome") or ""),
            effect_source_ref=str(payload.get("effect_source_ref") or ""),
            handler_executed=payload.get("handler_executed") is True,
            failure_stage=str(payload.get("failure_stage") or ""),
            duration_ms=payload.get("duration_ms") or 0,
        )
    except (TypeError, ValueError):
        return _operation_error(
            record.tool,
            "TOOL_OPERATION_OUTCOME_UNKNOWN",
            "已有副作用操作结果含无效执行诊断字段，系统不会重复执行。",
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
    *,
    reported_error_code: str = "",
) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name,
        False,
        json.dumps(
            {"ok": False, "error": message, "error_code": error_code},
            ensure_ascii=False,
        ),
        error_code=error_code,
        reported_error_code=reported_error_code,
    )


def _attach_operation_facts(
    result: ToolExecutionResult,
    request: ToolOperationExecutionRequest,
    *,
    status: str,
    action: str,
    replayed: bool = False,
    diagnostic: str = "",
    source_ref: str = "",
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
    if source_ref:
        facts["reconciliation_source_ref"] = str(source_ref)[:240]
    result.result_envelope.setdefault("tool_operation", {}).update(facts)


__all__ = [
    "ToolOperationExecutionRequest",
    "execute_tool_operation",
]
