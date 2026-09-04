from __future__ import annotations

"""Authoritative execution lifecycle for side-effecting tools.

The runtime gate decides whether a call may run.  This coordinator decides
whether this exact operation may run *now*.  Its durable claim is written
before the tool implementation is entered; audit ledgers remain best-effort
and are deliberately not consulted for execution authority.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

from ..local_storage import (
    TOOL_OPERATION_FAILED,
    TOOL_OPERATION_SUCCEEDED,
    TOOL_OPERATION_UNKNOWN,
    ToolOperationClaim,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationHolder,
    ToolOperationReconciliationClaimRequest,
    ToolOperationRecord,
    ToolOperationReopenRequest,
    new_tool_operation_holder,
)
from ..runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    RuntimeConflictError,
)
from .models import (
    ToolFailureStage,
    ToolHandlerOutcome,
    ToolOperationReconciliation,
    apply_tool_execution_facts,
)

_RESULT_SCHEMA = "tool_execution_result.v1"
_MINIMUM_LEASE_SECONDS = 900
_LEASE_GRACE_SECONDS = 300

# S-D1（2026-08-20 SUB-D 真机）：workspace 锁冲突（另一执行者持有同一 scope
# 的 claim）是瞬态并发语义——高频派工/并行创建子代理时，前一个操作通常
# 几百毫秒到几秒内释放。直接返回 BUSY_CONFLICT 会让 create_subagents 整批
# 失败且从不重试（b1_1 实锤）。这里做有界短重试：3 次 × 0.5s/1s/2s 递增，
# 重试耗尽才报冲突。
_BUSY_RETRY_ATTEMPTS = 3
_BUSY_RETRY_DELAYS = (0.5, 1.0, 2.0)


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
    invoke: Callable[[], ToolHandlerOutcome]
    reconcile: Callable[[ToolOperationRecord], ToolOperationReconciliation] | None = None
    resource_scopes: tuple[str, ...] = ()
    # seq 245 P2：调用者 attempt 身份透传（来源 = ToolCall.attempt_id）。
    attempt_id: str = ""


@dataclass(frozen=True)
class _ToolOperationClaimAttempt:
    claim: ToolOperationClaim
    holder: ToolOperationHolder
    lease_expires_at: float


def execute_tool_operation(
    request: ToolOperationExecutionRequest,
) -> ToolHandlerOutcome:
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
    if isinstance(attempt, ToolHandlerOutcome):
        return attempt
    resolved = _resolve_claim(request, attempt)
    if isinstance(resolved, ToolHandlerOutcome):
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
) -> _ToolOperationClaimAttempt | ToolHandlerOutcome:
    holder = new_tool_operation_holder()
    lease_seconds = max(
        _MINIMUM_LEASE_SECONDS,
        max(0, int(request.timeout_seconds or 0)) + _LEASE_GRACE_SECONDS,
    )
    lease_expires_at = time.time() + lease_seconds
    conflict_error: RuntimeConflictError | None = None
    for attempt in range(_BUSY_RETRY_ATTEMPTS + 1):
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
                    resource_scopes=request.resource_scopes,
                    attempt_id=request.attempt_id,
                )
            )
            conflict_error = None  # 重试成功：清除之前记录的冲突
            break
        except RuntimeConflictError as exc:
            # S-D1: 锁冲突是瞬态并发语义——有界短重试（0.5s/1s/2s），
            # 重试耗尽才映射为 BUSY_CONFLICT（高频派工不再整批失败）。
            conflict_error = exc
            if attempt >= _BUSY_RETRY_ATTEMPTS:
                break
            time.sleep(_BUSY_RETRY_DELAYS[attempt])
        except Exception as exc:  # noqa: BLE001 - authoritative store failures fail closed
            authority_missing = isinstance(exc, AuthorityContextMissing)
            result = _operation_error(
                request.tool_name,
                "TOOL_AUTHORITY_CONTEXT_MISSING"
                if authority_missing
                else "TOOL_OPERATION_STORE_UNAVAILABLE",
                "MANAGED 权威链缺失，无法建立执行占位，工具没有运行。"
                if authority_missing
                else "副作用操作账本无法建立执行占位，工具没有运行。",
            )
            _attach_operation_facts(
                result,
                request,
                status="not_started",
                action="authority_missing" if authority_missing else "store_unavailable",
                diagnostic=type(exc).__name__,
            )
            return apply_tool_execution_facts(
                result,
                failure_stage=ToolFailureStage.PERSISTENCE,
                handler_executed=False,
            )
    if conflict_error is not None:
        # WRITE-03(2026-08-15 真机): 执行权/资源锁冲突是可预期并发语义(另一执行者
        # 持有锁/attempt 已换代), 不是"账本不可用"——映射为可重试冲突码, 避免把
        # 并发冲突误报成存储故障(子代理场景真机 UNAVAILABLE 掩盖了真实语义)。
        # S-D1 延伸：已做有界短重试仍冲突才到这里。
        exc = conflict_error
        result = _operation_error(
            request.tool_name,
            "TOOL_OPERATION_BUSY_CONFLICT",
            f"工具执行冲突（另一执行者正在处理同一操作，已重试 {_BUSY_RETRY_ATTEMPTS} 次仍冲突）: {exc}",
            reported_error_code="TOOL_OPERATION_BUSY_CONFLICT",
        )
        _attach_operation_facts(
            result,
            request,
            status="not_started",
            action="busy_conflict",
            diagnostic=type(exc).__name__,
        )
        return apply_tool_execution_facts(
            result,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
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
) -> ToolOperationClaim | ToolHandlerOutcome:
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
) -> ToolOperationClaim | ToolHandlerOutcome:
    claim = attempt.claim
    if request.reconcile is None:
        return _unknown_claim_result(request, claim, diagnostic=claim.reason or "no_reconciler")
    claimed = _claim_unknown_reconciliation(request, attempt)
    if isinstance(claimed, ToolHandlerOutcome):
        return claimed
    attempt = claimed
    claim = attempt.claim
    try:
        reconciliation = request.reconcile(claim.record)
    except Exception as exc:  # noqa: BLE001 - reconciliation is read-only and must fail closed
        _release_unknown_reconciliation(request, attempt)
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
        _release_unknown_reconciliation(request, attempt)
        return _unknown_claim_result(
            request,
            claim,
            diagnostic=f"invalid_reconciliation_outcome:{outcome or 'empty'}",
        )
    if outcome == "unknown":
        _release_unknown_reconciliation(request, attempt)
        return _unknown_claim_result(
            request,
            claim,
            diagnostic=str(reconciliation.reason or claim.reason or "still_unknown"),
            source_ref=source_ref,
        )
    if not source_ref:
        _release_unknown_reconciliation(request, attempt)
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
    return _settle_reconciled_operation(
        request,
        attempt,
        reconciliation,
        source_ref=source_ref,
    )


# LLM: A terminal reconciliation result becomes authoritative only after the same exact
# reconciliation holder/generation settles the operation store. Persistence failure stays UNKNOWN.
# 函数用途: 校验核对结果并把已证明的成功或失败终态写回权威操作账本。
def _settle_reconciled_operation(
    request: ToolOperationExecutionRequest,
    attempt: _ToolOperationClaimAttempt,
    reconciliation: ToolOperationReconciliation,
    *,
    source_ref: str,
) -> ToolHandlerOutcome:
    claim = attempt.claim
    outcome = str(reconciliation.outcome or "").strip().lower()
    reconciled_result = reconciliation.result
    if reconciled_result is None or reconciled_result.ok != (outcome == "succeeded"):
        _release_unknown_reconciliation(request, attempt)
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
        _release_unknown_reconciliation(request, attempt)
        return _unknown_claim_result(
            request,
            claim,
            diagnostic=f"reconciliation_persistence_failed:{type(exc).__name__}",
            source_ref=source_ref,
        )
    return reconciled_result


# LLM: Reconciliation handlers may persist receipts, so UNKNOWN inspection is itself an exclusive
# operation. A store without this atomic contract must fail closed instead of falling back to an
# in-process mutex that cannot protect two Gateways/TUIs.
# 函数用途: 通过权威 operation store 原子领取未知副作用的核对权。
def _claim_unknown_reconciliation(
    request: ToolOperationExecutionRequest,
    attempt: _ToolOperationClaimAttempt,
) -> _ToolOperationClaimAttempt | ToolHandlerOutcome:
    claim_reconciliation = getattr(
        request.store,
        "claim_tool_operation_reconciliation",
        None,
    )
    if not callable(claim_reconciliation):
        return _unknown_claim_result(
            request,
            attempt.claim,
            diagnostic="operation_store_reconciliation_claim_missing",
        )
    claim_request = _reconciliation_claim_request(attempt)
    try:
        claimed = claim_reconciliation(claim_request)
    except Exception as exc:  # noqa: BLE001 - claim authority failure leaves UNKNOWN unchanged
        return _unknown_claim_result(
            request,
            attempt.claim,
            diagnostic=f"reconciliation_claim_failed:{type(exc).__name__}",
        )
    if claimed.action != "reconcile":
        return _unknown_claim_result(
            request,
            claimed,
            diagnostic=claimed.reason or f"reconciliation_claim_{claimed.action}",
        )
    return _ToolOperationClaimAttempt(
        claim=ToolOperationClaim("unknown", claimed.record, attempt.claim.reason),
        holder=attempt.holder,
        lease_expires_at=attempt.lease_expires_at,
    )


# LLM: Only the exact reconciliation holder/generation may release. A release race is diagnostic;
# it never grants execution and never replaces a terminal fact written by another process.
# 函数用途: 核对仍无终态或异常时尽力释放权威核对租约。
def _release_unknown_reconciliation(
    request: ToolOperationExecutionRequest,
    attempt: _ToolOperationClaimAttempt,
) -> None:
    release = getattr(request.store, "release_tool_operation_reconciliation", None)
    if not callable(release):
        return
    try:
        release(_reconciliation_claim_request(attempt))
    except Exception:  # noqa: BLE001 - UNKNOWN and lease expiry remain the safe fallback
        logger.warning("tool operation reconciliation release failed", exc_info=True)


# LLM: Claim and release must use the identical structured holder, generation, and lease values.
# 函数用途: 从当前 UNKNOWN claim 构造核对租约请求。
def _reconciliation_claim_request(
    attempt: _ToolOperationClaimAttempt,
) -> ToolOperationReconciliationClaimRequest:
    record = attempt.claim.record
    return ToolOperationReconciliationClaimRequest(
        owner_id=record.owner_id,
        run_id=record.run_id,
        operation_id=record.operation_id,
        expected_generation=record.generation,
        holder=attempt.holder,
        lease_expires_at=attempt.lease_expires_at,
    )


def _reopen_unknown_operation(
    request: ToolOperationExecutionRequest,
    attempt: _ToolOperationClaimAttempt,
    *,
    source_ref: str,
    reconciliation_outcome: str,
) -> ToolOperationClaim | ToolHandlerOutcome:
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
) -> ToolHandlerOutcome:
    retry_safe = reconciliation_outcome == "safe_to_retry"
    result = ToolHandlerOutcome(
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
) -> ToolHandlerOutcome:
    prior = _result_from_record(claim.record)
    reported = (
        prior.reported_error_code
        if prior.error_code != "TOOL_OPERATION_OUTCOME_UNKNOWN"
        else claim.record.error_code
    )
    # 本次未执行(claim 拦截,handler 没被调用)是明确事实——不能算「本次结果未知」。
    # effect_outcome=not_started 让 executor 的最终归档(status=failed)与模型看到的
    # 语义一致:「上次执行可能已生效,本次没跑且不重做」;错误码保持 OUTCOME_UNKNOWN
    # 防重做契约不变。prior_effect_outcome 是上次结果的旁证,不篡改历史记录。
    result = _operation_error(
        request.tool_name,
        "TOOL_OPERATION_OUTCOME_UNKNOWN",
        "此前执行可能已经产生副作用，但目标系统尚未给出可证明的终态；本次调用未执行，系统不会自动重做，请人工核验目标系统状态。",
        reported_error_code=reported,
    )
    result.effect_outcome = "not_started"
    result.result_envelope["reported_tool_result"] = _reported_tool_result_facts(prior)
    result.result_envelope["prior_effect_outcome"] = (
        prior.effect_outcome or claim.record.status
    )
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
) -> ToolHandlerOutcome:
    result = _invoke_with_lease_renewal(request, claim)
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
    reported: ToolHandlerOutcome,
) -> ToolHandlerOutcome:
    envelope = dict(reported.result_envelope or {})
    envelope["reported_tool_result"] = _reported_tool_result_facts(reported)
    return ToolHandlerOutcome(
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


def _invoke_with_lease_renewal(
    request: ToolOperationExecutionRequest,
    claim: ToolOperationClaim,
) -> ToolHandlerOutcome:
    """invoke 期间续租守护线程（seq 258 #1：renew 生产调用链）。

    长 handler（进程执行/等待）阻塞超过 lease 时，另一请求会按 lease 过期把
    本 EXECUTING 行标 UNKNOWN 并释放锁 → 竞态抢锁。续租线程在 lease 过半前
    滚动续租（holder CAS + workspace_epoch CAS 由 store 原语保证），invoke
    一返回立即停止——handler 结果与续租成败无关（续不动 = 锁自然过期，
    行为不劣化）。LOCAL store 无续租契约（结构化信号）→ 行为不变。
    """
    renew = getattr(request.store, "renew_tool_operation_lease", None)
    if not callable(renew):
        return request.invoke()
    record = claim.record
    remaining = max(0.0, float(record.lease_expires_at or 0) - time.time())
    interval = max(0.5, remaining / 2.0)
    stop = threading.Event()

    def renew_loop() -> None:
        while not stop.wait(interval):
            try:
                renew(
                    operation_id=record.operation_id,
                    holder_id=record.holder_id,
                    lease_expires_at=time.time() + interval * 2 + 5,
                    owner_id=record.owner_id,
                    run_id=record.run_id,
                )
            except Exception:  # noqa: BLE001 - 续租失败只停线程，不影响 handler
                logger.warning("tool operation lease renewal stopped", exc_info=True)
                return

    thread = threading.Thread(target=renew_loop, daemon=True)
    thread.start()
    try:
        return request.invoke()
    finally:
        stop.set()
        thread.join(timeout=5)


def _operation_status_for_result(result: ToolHandlerOutcome) -> str:
    if result.ok:
        return TOOL_OPERATION_SUCCEEDED
    # failed=handler 结构化声明的确定性失败(如进程完整退出自报退出码,
    # 2026-08-15 长代码真机: unittest 校验失败被归 UNKNOWN 导致任务死)。
    # 与 not_started 同属「结果是确定的」——终态 FAILED, 模型可读输出修正,
    # 不触发 unknown 收口闸; 失败可能部分生效由模型观测文件/输出裁决。
    if result.effect_outcome in {"not_started", "failed"}:
        return TOOL_OPERATION_FAILED
    # 问题B(2026-08-14 真机实证, ④类复刻): handler 显式返回的「执行前确定性
    # 失败」(taxonomy category=tool/path 且 retryable——参数/路径类, 副作用在
    # 校验阶段就未发生) 即使 execute_authorized_tool 标了 handler_executed=True
    # 也按 FAILED 终态归档。修复前一律 UNKNOWN → 系统禁止自动重试 → 模型
    # 卡死到轮限(真机 outcome_json: effect_outcome_unknown:TOOL_INVALID_ARGUMENTS,
    # apply_patch 无效补丁卡死整个复刻任务)。
    if _is_deterministic_pre_handler_failure(result):
        return TOOL_OPERATION_FAILED
    # Once a mutating/dangerous handler was entered, a generic failure cannot
    # prove that no side effect happened.  Only an explicit structured
    # ``not_started`` fact may make the operation terminal-failed; error text
    # and taxonomy labels never grant replay authority.
    if result.effect_outcome == "unknown" or result.handler_executed:
        return TOOL_OPERATION_UNKNOWN
    return TOOL_OPERATION_FAILED


# 执行前确定性失败族: handler 在副作用前显式返回的校验类错误码白名单——
# 参数/路径/写入边界在触碰任何副作用目标前失败, 零副作用是结构化机器事实,
# 即使 execute_authorized_tool 标了 handler_executed=True 也应终态 FAILED。
# 不能用 taxonomy 动作/category 当判据: TOOL_TIMEOUT 与 TOOL_ERROR 也是
# category=tool+retryable(+REPAIR_TOOL_ARGUMENTS), 但它们「可能已生效」
# 必须保持 UNKNOWN(禁止自动重试)。白名单=只含校验阶段专属码, 新增校验码
# 须同步登记(漏登=保守回 UNKNOWN, 安全方向)。
#
# OWNER_QUOTA_UNAVAILABLE(2026-08-14 真机, bs4 复刻): owner 配额无法可靠读取
# 时写入 fail-closed、副作用零发生(owner_quota_error_result 在触碰任何文件前
# 显式拒绝), taxonomy=state/retryable——与 TOOL_INVALID_ARGUMENTS 同属「执行前
# 确定性失败」族。修复前归 UNKNOWN → 禁止自动重试 → bs4 最后一笔 write_file
# (fix5.py)因此卡死到轮限(真机 outcome_json=effect_outcome_unknown:
# OWNER_QUOTA_UNAVAILABLE)。归 FAILED 后模型可如实报告/换策略, 不再哑卡。
# OWNER_DISK_QUOTA_EXCEEDED 同样 fail-closed, 但可能部分生效语义未决,
# 由 effect_outcome/其他路径裁决, 不进本白名单。
_PRE_HANDLER_DETERMINISTIC_CODES = frozenset(
    {
        "TOOL_INVALID_ARGUMENTS",
        "TOOL_INTERNAL_PARAMETER_FORBIDDEN",
        "PATH_OUTSIDE_WORKSPACE",
        "PATH_NOT_FOUND",
        "WRITE_FORBIDDEN",
        "PERSONA_WRITE_REQUIRES_TOOL",
        "OWNER_QUOTA_UNAVAILABLE",
    }
)


# LLM: 白名单是「校验阶段确定性拒绝」的唯一权威——判定不得再叠加 taxonomy
# retryable 闸。EXEC-03(2026-08-15 compact 场景真机): PATH_OUTSIDE_WORKSPACE
# (path/retryable=False)与 WRITE_FORBIDDEN(permission/retryable=False)是
# 校验阶段零副作用拒绝, 但旧判定 `code in 白名单 and contract.retryable`
# 使这两个码永不命中 → 归 UNKNOWN → unknown 收口闸 → 模型无法改路径重试,
# 任务死。终态 FAILED 不授予自动重试(同参重放仍回 FAILED 不重执行), 只是
# 解除 UNKNOWN 收口, 让模型读 recovery_hint 改参后以新调用继续——这正是
# retryable=False 的语义(不自动重试同一操作, 允许人工/模型换参重来)。
def _is_deterministic_pre_handler_failure(result: ToolHandlerOutcome) -> bool:
    code = str(result.error_code or "").upper()
    return code in _PRE_HANDLER_DETERMINISTIC_CODES


def _unknown_outcome_result(
    request: ToolOperationExecutionRequest,
    reported: ToolHandlerOutcome,
) -> ToolHandlerOutcome:
    reported_code = (
        reported.reported_error_code
        or reported.error_code
        or "UNKNOWN_ERROR"
    )
    envelope = dict(reported.result_envelope or {})
    envelope["reported_tool_result"] = _reported_tool_result_facts(reported)
    # 2026-08-15 3×3 cell1 真机: COMMAND_FAILED(写命令失败, handler 真实执行
    # 过)被 UNKNOWN 包装时原始失败输出被丢弃——模型拿不到编译错误文本无法
    # 修复, 任务 failed 死路。给模型的 output 保留有界摘要(截断防大正文/
    # 密钥), 模型可见失败原因; 账本侧(_reported_tool_result_facts)保持
    # 不含 output 的安全归档不变。
    reported_preview = str(reported.output or "")[:2000]
    return ToolHandlerOutcome(
        tool=request.tool_name,
        ok=False,
        output=json.dumps(
            {
                "ok": False,
                "error": "工具调用已结束等待，但副作用是否完成仍不确定；系统已阻止自动重做。",
                "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN",
                "reported_error_code": reported_code,
                "reported_output_preview": reported_preview,
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
    reported: ToolHandlerOutcome,
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


def _tool_execution_facts(result: ToolHandlerOutcome) -> dict[str, object]:
    facts: dict[str, object] = {
        "handler_executed": result.handler_executed,
        "duration_ms": result.duration_ms,
    }
    if result.failure_stage:
        facts["failure_stage"] = result.failure_stage
    return facts


def _result_payload(result: ToolHandlerOutcome) -> dict[str, Any]:
    return {
        "schema_version": _RESULT_SCHEMA,
        "result_ref": _operation_result_ref(result),
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


def _operation_result_ref(result: ToolHandlerOutcome) -> str:
    operation = result.result_envelope.get("tool_operation")
    return (
        str(operation.get("result_ref") or "").strip()
        if isinstance(operation, dict)
        else ""
    )


def _result_from_record(record: ToolOperationRecord) -> ToolHandlerOutcome:
    payload = record.result if isinstance(record.result, dict) else {}
    if payload.get("schema_version") != _RESULT_SCHEMA:
        return _operation_error(
            record.tool,
            "TOOL_OPERATION_OUTCOME_UNKNOWN",
            "已有副作用操作缺少可重放的完整结果，系统不会重复执行。",
        )
    try:
        result = ToolHandlerOutcome(
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
) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
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
    result: ToolHandlerOutcome,
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
        "result_ref": (
            f"tool-operation://{request.run_id}/{request.operation_id}"
        ),
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
