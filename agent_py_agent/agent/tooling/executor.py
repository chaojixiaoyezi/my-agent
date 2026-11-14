# LLM: 管理员全盘事实默认 False，仅运输 registry 的当次只读裁决，不从模型或恢复 JSON 取得。
# LLM: 共用唯一执行状态机；来源/征询和 guarded 展示均由宿主注入，观察有界收口，不扩大权限或模型参数。
# 模块用途: 统一工具校验、授权、执行和结果记录，区分未执行、已执行与副作用未知。
from __future__ import annotations

"""The only state machine allowed to enter a registered tool handler."""

import difflib
import errno
import hashlib
import json
import logging
import math
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from functools import partial
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

from ..common.cancellation import CancellationToken
from ..common.log_redaction import redact_sensitive_text, redact_sensitive_value
from ..contracts.diagnostics import DiagnosticEnvelope
from ..contracts.error_taxonomy import error_contract
from ..contracts.idempotency import BusinessEffectIdentity
from ..io.sqlite_connection import sqlite_error_details
from ..local_storage import ToolOperationRecord
from ..plugin_events.decision_ledger import PLUGIN_GATE_DECISION_EVIDENCE_KEY
from ..plugin_events.points import EventPointContext
from ..runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ToolOperationAuthorityRequest,
)
from .action_policy import ActionDecision, ActionPolicy, ActionPolicyRequest
from .event_observation import ToolEventObservation
from .liveness import ActivityHeartbeat
from .models import (
    ResourceScopeResolutionError,
    ToolAvailability,
    ToolFailureStage,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolOperationReconciliation,
    ToolOperationReconciliationContext,
    ToolOperationSettlementContext,
    ToolRuntime,
    ToolRuntimeSnapshot,
    apply_tool_execution_facts,
    output_policy_for_outcome,
)
from .output_projection import project_tool_output_body
from .registry_invoke import (
    RegistryToolInvokeRequest,
    _managed_generation_denial,
    invoke_registry_tool,
)
from .registry_workspace import effective_registry_cwd
from .runtime_boundary import canonicalize_owner_home_arguments
from .runtime_contracts import (
    ToolCall,
    ToolContentBlock,
    ToolFailureFacts,
    ToolOperation,
    ToolResult,
    ToolResultRef,
    ToolSuccessFacts,
)
from .tool_input_completion import (
    ToolInputCompletionContext,
    ToolInputSource,
    complete_tool_arguments,
)
from .tool_operation_coordinator import (
    ToolOperationExecutionRequest,
    execute_tool_operation,
)


@dataclass(frozen=True)
class ToolOutputProjection:
    content_blocks: tuple[ToolContentBlock, ...]
    refs: tuple[ToolResultRef, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolExecution:
    call: ToolCall
    decision: ActionDecision
    result: ToolResult
    states: tuple[str, ...]


# LLM: 模型参数与宿主配置分离；审批、事件身份、调用来源和征询回调只由可信调用方设置，来源默认 model，不能从 ToolCall 参数提权。
# 类用途: 汇总本次调用与取消上下文；typed guarded/单参观察回调只择一，不改变权限或工具结果。
# LLM: 管理员 Full Access 仅由 registry 当次原 owner 与有效权限投影注入，默认 False；工具模型参数和归档不得提供。联查 ActionPolicyRequest 与 registry。
# 类用途: 保存唯一执行器本次调用与宿主权限快照，不执行副作用。
@dataclass(frozen=True)
class ToolExecutorRequest:
    call: ToolCall
    runtime_snapshot: ToolRuntimeSnapshot
    workspace_root: Path
    workspace_roots: tuple[Path, ...] = ()
    path_access_mode: str = "normal"
    path_dangerous_roots: tuple[str, ...] = ()
    owner_scope_root: str = ""
    write_boundary: dict[str, object] | None = None
    runtime_guard_policy: object | None = None
    approval_mode: str = "ask"
    operation_store: object | None = None
    operation_store_required: bool = True
    operation_owner_id: str = "local/main"
    owner_type: str = "main_agent"
    trusted_run_context: dict[str, object] | None = None
    required_action: object | None = None
    cancellation_token: CancellationToken | None = None
    pre_handler_gate: Callable[[ToolCall], ToolHandlerOutcome | None] | None = None
    output_archiver: Callable[[ToolCall, object], ToolOutputProjection] | None = None
    require_model_visibility: bool = True
    event_context: EventPointContext | None = None
    on_handler_started: Callable[[], None] | None = None
    on_handler_progress: Callable[[dict[str, object]], None] | None = None
    on_handler_progress_guarded: Callable[..., None] | None = None
    handler_progress_interval_seconds: float = 120.0
    # 来源与征询提供方仅由宿主注入；模型参数没有任何来源控制权。
    allow_nested_dispatch: bool = True
    call_origin: str = "model"
    plugin_gate_reviewer: Callable | None = None
    admin_full_access: bool = False
    pending_input_probe: Callable[[], bool] | None = field(default=None, repr=False, compare=False)
    business_actor: object | None = field(default=None, kw_only=True, repr=False, compare=False)
    business_context: object | None = field(default=None, kw_only=True, repr=False, compare=False)
    business_effect: BusinessEffectIdentity | None = field(default=None, kw_only=True)


# LLM: 同一授权裁决、输入来源和计时沿执行和前置失败出口共用；成组传递避免另建授权或多参数接口。
# 类用途: 保存一次已授权工具执行共用的裁决、计时、输入来源和状态轨迹。
@dataclass(frozen=True)
class _AuthorizedExecutionProgress:
    sources: tuple[ToolInputSource, ...]
    started_at: float
    states: list[str]
    decision: ActionDecision


# LLM: 唯一 handler 入口；B5 在宿主 decide 后、原审批前收紧，B4 观察只跟随真实 handler，原资源和操作账顺序不得被旁路。
# 类用途: 用同一状态机执行或前置拒绝工具，插件不接管执行，未执行调用不伪造工具观察事件。
class ToolExecutor:
    # LLM: 构造不启动通道或执行工具；策略缺省仍沿原 ActionPolicy，征询来自每次可信请求而非模型参数。
    # 函数用途: 保存宿主策略，等待实际调用沿唯一执行链核对。
    def __init__(self, policy: ActionPolicy | None = None) -> None:
        self.policy = policy or ActionPolicy()

    # LLM: 协议代次只读宿主trusted_run_context；v1归一与已退役叶子别名解码均发生在schema/effect/审批/claim前，不变更调用ID与幂等键。
    # 函数用途: 在唯一执行链前解码旧输入与已退役业务别名，保留原请求迁移事实而不重执行历史。
    def execute(self, request: ToolExecutorRequest) -> ToolExecution:
        from .versioned_input_decoder import (
            dead_business_spaces_redirect,
            decode_retired_tool_call,
            decode_tool_input,
        )

        original = request.call
        snapshot = request.runtime_snapshot
        version = (request.trusted_run_context or {}).get("tool_input_version", 2)
        is_current_v2 = version == 2 and type(version) is int
        migration: dict[str, Any] = dict(original.input_migration) if original.input_migration else {}
        if is_current_v2:
            name, arguments = original.tool_name, original.arguments
        else:
            try:
                name, arguments = decode_tool_input(original.tool_name, original.arguments, version=version)
            except (TypeError, ValueError) as exc:
                safe_summary = redact_sensitive_text(str(exc)[:500])
                decoder_err = {"exception_type": type(exc).__name__, "summary": safe_summary}
                decision = ActionDecision(
                    "deny",
                    ("TOOL_INVALID_ARGUMENTS",),
                    {
                        "failure_stage": "validation",
                        "decoder_error": decoder_err,
                        "error_details": {"decoder_error": decoder_err},
                        "boundary_error": f"{type(exc).__name__}: {safe_summary}",
                    },
                )
                return ToolExecution(
                    original,
                    decision,
                    _decision_result(original, decision, time.monotonic()),
                    ("received", "validation_failed", "projected"),
                )
            migration = {
                "schema": "tool_input_migration.v1",
                "input_version": version,
                "requested_tool_name": original.tool_name,
                "canonical_tool_name": name,
                "original_schema_hash": original.schema_hash,
                "original_args_hash": original.args_hash,
                "action": arguments.get("action", "") if isinstance(arguments, dict) else "",
                "call_id_preserved": True,
                **({"prior_migration": dict(original.input_migration)} if original.input_migration else {}),
            }
            if name == "create_subagents":
                raw_items = original.arguments.get("items")
                source_items = raw_items if raw_items else [original.arguments]
                migration["create_projection"] = {
                    "single": not bool(raw_items),
                    "item_allowed_tools_explicit": ["allowed_tools" in item for item in source_items],
                }

        if name == "business_spaces" and "business_spaces" not in snapshot.available_tool_names:
            redirect = dead_business_spaces_redirect(arguments if isinstance(arguments, dict) else {})
            err_details = {k: v for k, v in redirect.items() if k != "next_action"}
            target_label = redirect.get("target_tool") or redirect.get("target_tools") or "business_space"
            decision = ActionDecision(
                "deny",
                ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",),
                {
                    "failure_stage": "authorization",
                    "reported_error_code": "TOOL_RETIRED_REDIRECT",
                    "unavailable_reason": (
                        f"已退役工具 business_spaces 与现行操作回执身份不兼容，不可直接执行；请改用 {target_label}"
                    ),
                    "error_details": err_details,
                    "next_action": str(redirect["next_action"]),
                },
            )
            return ToolExecution(
                original,
                decision,
                _decision_result(original, decision, time.monotonic()),
                ("received", "persisted", "projected"),
            )

        if name not in snapshot.available_tool_names:
            try:
                alias_decoded = decode_retired_tool_call(
                    name,
                    arguments,
                    available=snapshot.available_tool_names,
                )
            except (TypeError, ValueError) as exc:
                safe_summary = redact_sensitive_text(str(exc)[:500])
                decision = ActionDecision(
                    "deny",
                    ("TOOL_INVALID_ARGUMENTS",),
                    {
                        "failure_stage": "validation",
                        "boundary_error": safe_summary,
                        "error_details": {
                            "exception_type": type(exc).__name__,
                            "summary": safe_summary,
                            "requested_tool_name": original.tool_name,
                        },
                    },
                )
                return ToolExecution(
                    original,
                    decision,
                    _decision_result(original, decision, time.monotonic()),
                    ("received", "validation_failed", "projected"),
                )
            if alias_decoded is not None:
                target, migrated_args, alias_action = alias_decoded
                denial = _retired_alias_target_denial(
                    snapshot,
                    requested_name=original.tool_name,
                    target=target,
                    action=alias_action,
                )
                if denial is not None:
                    return ToolExecution(
                        original,
                        denial,
                        _decision_result(original, denial, time.monotonic()),
                        ("received", "persisted", "projected"),
                    )
                prior = dict(original.input_migration) if original.input_migration else {}
                migration = {
                    "schema": "tool_input_migration.v1",
                    "input_version": version,
                    "requested_tool_name": original.tool_name,
                    "canonical_tool_name": target,
                    "original_schema_hash": original.schema_hash,
                    "original_args_hash": original.args_hash,
                    "action": alias_action,
                    "call_id_preserved": True,
                    **({"prior_migration": prior} if prior else {}),
                }
                name, arguments = target, migrated_args

        if is_current_v2 and name == original.tool_name and arguments is original.arguments:
            return self._execute_current(request)
        runtime = snapshot.runtime(name)
        call = replace(
            original,
            tool_name=name,
            arguments=arguments,
            input_migration=migration,
            schema_hash=runtime.model_spec.schema_hash if runtime is not None else original.schema_hash,
        )
        return self._execute_current(replace(request, call=call))

    # LLM: 规范请求沿原唯一执行状态机；当前权限再核验，归一不能跳过B5、审批或原操作账。
    # 函数用途: 校验规范调用并执行或前置拒绝。
    def _execute_current(self, request: ToolExecutorRequest) -> ToolExecution:
        started_at = time.monotonic()
        states = ["received"]
        runtime = request.runtime_snapshot.runtime(request.call.tool_name)
        if runtime is None:
            decision = _missing_snapshot_runtime_decision(request.runtime_snapshot, request.call.tool_name)
            result = _decision_result(request.call, decision, started_at)
            return ToolExecution(
                request.call,
                decision,
                result,
                (*states, "persisted", "projected"),
            )

        call, sources, normalization_error, normalization_details = _normalized_call(request, runtime)
        states.append("normalized")
        if normalization_error:
            _LOGGER.warning(
                "tool validation failed: tool=%s run=%s turn=%s error=%s "
                "arguments_head=%r",
                str(request.call.tool_name or ""),
                str(getattr(request.call, "run_id", "") or ""),
                str(getattr(request.call, "turn_id", "") or ""),
                normalization_error,
                json.dumps(redact_sensitive_value(request.call.arguments), ensure_ascii=False)[:500],
            )
            evidence: dict[str, object] = {"failure_stage": ToolFailureStage.VALIDATION.value}
            if normalization_details:
                evidence["error_details"] = normalization_details
            decision = ActionDecision("deny", (normalization_error,), evidence)
            result = _decision_result(call, decision, started_at, input_sources=sources)
            return ToolExecution(call, decision, result, (*states, "persisted", "projected"))

        if _cancelled(request):
            decision = ActionDecision("deny", ("CANCELLED",), {"failure_stage": "runtime_gate"})
            result = ToolResult.failed(
                call,
                "The tool call was cancelled before the handler started.",
                error_code="CANCELLED",
                failure_stage="runtime_gate",
                facts=ToolFailureFacts(
                    duration_ms=_elapsed_ms(started_at),
                    status="cancelled",
                    metadata={"input_sources": _source_dicts(sources)},
                ),
            )
            return ToolExecution(
                call,
                decision,
                result,
                (*states, "cancelled", "persisted", "projected"),
            )

        decision = self.policy.decide(
            ActionPolicyRequest(
                call=call,
                runtime_snapshot=request.runtime_snapshot,
                workspace_root=request.workspace_root,
                workspace_roots=request.workspace_roots,
                path_access_mode=request.path_access_mode,
                path_dangerous_roots=request.path_dangerous_roots,
                owner_scope_root=request.owner_scope_root,
                write_boundary=request.write_boundary,
                runtime_guard_policy=request.runtime_guard_policy,
                approval_mode=request.approval_mode,
                admin_full_access=request.admin_full_access,
                owner_id=request.operation_owner_id,
                required_action=request.required_action,
                require_model_visibility=request.require_model_visibility,
                business_actor=request.business_actor,
                business_context=request.business_context,
            )
        )
        from .plugin_gate_policy import tighten_plugin_decision

        decision = tighten_plugin_decision(request, call, decision)
        if decision.status == "ask":
            decision = _precheck_before_approval(runtime, decision)
        states.extend(("validated", "authorized"))
        if not decision.allowed:
            states.append("approval_pending" if decision.status == "ask" else "failed")
            result = _decision_result(call, decision, started_at, input_sources=sources)
            return ToolExecution(call, decision, result, (*states, "persisted", "projected"))

        return _execute_authorized(
            request,
            call,
            _AuthorizedExecutionProgress(sources, started_at, states, decision),
        )


# LLM: 授权后仍可能被 validator、权限复核或幂等重放拦住；事件开始回调只沿真实 handler 入口传递，不提前发布。
# 函数用途: 执行已放行调用并收口结果，真实 handler 的心跳必在终态前停止，观察故障不改变授权、操作账本或返回值。
def _execute_authorized(
    request: ToolExecutorRequest,
    call: ToolCall,
    progress: _AuthorizedExecutionProgress,
) -> ToolExecution:
    """Execute a call only after normalization and ActionPolicy authorization."""
    runtime = request.runtime_snapshot.runtime(call.tool_name)
    decision = progress.decision
    sources, started_at, states = progress.sources, progress.started_at, progress.states
    # 只对"经过用户审批"的调用在 claim 之前再复核一次，覆盖审批等待期间被停用的时序；免审批调用沿冻结快照，不逐次复核。
    if decision.evidence.get("approval_applied") is True:
        availability = _pre_execution_precheck(runtime)
        if availability is not None:
            denial = _precheck_denial(decision, availability, "post_approval")
            states.append("failed")
            return ToolExecution(call, denial, _decision_result(call, denial, started_at, input_sources=sources),
                                 (*states, "persisted", "projected"))
        # 审批等待之后、领取与handler之前复核受管原代次；失效时不进入handler。
        generation_outcome = _managed_generation_denial(call.tool_name, "tool_after_approval")
        if generation_outcome is not None:
            return _pre_handler_failure_execution(request, call, runtime, decision, progress, generation_outcome)
    states.append("approved")
    if decision.sandbox_plan:
        states.append("sandbox_prepared")
    validation_outcome = _preclaim_validation_outcome(request, call, runtime)
    if validation_outcome is not None:
        return _pre_handler_failure_execution(
            request,
            call,
            runtime,
            decision,
            progress,
            validation_outcome,
        )
    if request.pre_handler_gate is not None:
        gate_outcome = request.pre_handler_gate(call)
        if gate_outcome is not None:
            return _pre_handler_failure_execution(
                request,
                call,
                runtime,
                decision,
                progress,
                gate_outcome,
            )
    states.append("running")
    observation = ToolEventObservation(request.event_context, call, decision.resolved_effect)
    liveness = ActivityHeartbeat({"call_id": call.call_id, "tool": call.tool_name,
        "phase": "running", "status": "执行中", "wait_state": "handler_result"}, request.on_handler_progress,
        request.handler_progress_interval_seconds, request.cancellation_token, guarded_sink=request.on_handler_progress_guarded)
    # LLM: 开始事实只由 Registry 真正进入 handler 触发，原观察、宿主回调和心跳共用该边界。
    # 函数用途: 发布真实开始事件并启动可选展示，不提前把授权成功当成已执行。
    def handler_started() -> None:
        observation.start()
        liveness.start()
        if request.on_handler_started is not None:
            try:
                request.on_handler_started()
            except Exception:  # noqa: BLE001 - 开始观察不能更改实际工具结果
                pass
    observed_request = replace(request, on_handler_started=handler_started)
    try:
        outcome = _invoke_with_operation_policy(observed_request, call, runtime, decision, started_at)
    finally:
        liveness.close()
    states.append("succeeded" if outcome.ok else _outcome_state(outcome))
    states.append("reconciled")
    projection = _project_output(request, call, runtime, outcome)
    result = _canonical_result(
        call,
        runtime,
        decision,
        outcome,
        projection,
        duration_ms=_elapsed_ms(started_at),
        input_sources=sources,
    )
    if result.failure_stage == ToolFailureStage.PERSISTENCE.value:
        states.append("persistence_failed")
    states.extend(("persisted", "projected"))
    observation.finish(result)
    return ToolExecution(call, decision, result, tuple(states))


# LLM: 条件参数校验必须在 mutating operation claim 前完成；validator 是 deny-only 纯校验器，
# 任何异常或错误地返回成功都 fail-closed，不能退回 handler 后再把确定性错误误报成 unknown。
# 函数用途: 执行工具自己的条件参数校验，并统一补齐“未进入 handler、未触发副作用”的事实。
def _preclaim_validation_outcome(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime,
) -> ToolHandlerOutcome | None:
    try:
        outcome = runtime.handler.validate_invocation(
            _handler_arguments(request, call, runtime),
            ToolInvocationContext(
                runtime_snapshot=request.runtime_snapshot,
                cancellation_token=request.cancellation_token,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - validator 故障必须在副作用前 fail-closed
        _LOGGER.exception(
            "tool preclaim validator failed: tool=%s run=%s turn=%s",
            call.tool_name,
            call.run_id,
            call.turn_id,
        )
        safe_summary = redact_sensitive_text(str(exc)[:500])
        err_no = exc.errno if isinstance(exc, OSError) and type(exc.errno) is int else None
        outcome = ToolHandlerOutcome(
            call.tool_name,
            False,
            f"工具调用参数校验器故障，已在执行前拒绝：{type(exc).__name__}"
            + (f" ({safe_summary})" if safe_summary else ""),
            error_code="TOOL_INVOCATION_VALIDATOR_FAILED",
            reported_error_code="TOOL_INVOCATION_VALIDATOR_FAILED",
            result_envelope={
                "error_details": {
                    "exception_type": type(exc).__name__,
                    "summary": safe_summary,
                    "stage": ToolFailureStage.VALIDATION.value,
                    **({"errno": err_no} if err_no is not None else {}),
                }
            },
        )
    if outcome is None:
        return None
    if not isinstance(outcome, ToolHandlerOutcome) or outcome.ok:
        outcome = ToolHandlerOutcome(
            call.tool_name,
            False,
            "工具调用参数校验器返回了非法结果，已在执行前拒绝",
            error_code="TOOL_INVOCATION_VALIDATOR_CONTRACT_BROKEN",
            reported_error_code="TOOL_INVOCATION_VALIDATOR_CONTRACT_BROKEN",
            result_envelope={
                "error_details": {
                    "returned_type": type(outcome).__name__,
                    "returned_ok": bool(getattr(outcome, "ok", False)),
                    "stage": ToolFailureStage.VALIDATION.value,
                }
            },
        )
    if outcome.error_code in {"", "UNKNOWN_ERROR"}:
        val_contract = error_contract("TOOL_INVOCATION_VALIDATOR_FAILED")
        outcome.error_code = val_contract.code
        if outcome.reported_error_code in {"", "UNKNOWN_ERROR"}:
            outcome.reported_error_code = val_contract.code
        outcome.error_category = val_contract.category
        outcome.retryable = False
        outcome.recommended_action = val_contract.recommended_action
        outcome.recovery_hint = val_contract.recovery_hint
    outcome.effect_outcome = "not_started"
    return apply_tool_execution_facts(
        outcome,
        failure_stage=ToolFailureStage.VALIDATION,
        handler_executed=False,
    )


# LLM: validator 与既有 pre_handler_gate 共用同一 canonical 投影/持久化出口；调用方传入的
# outcome 已携带权威 failure_stage，不能在这里猜测或把未执行失败升级成副作用 unknown。
# 函数用途: 把 handler 前失败转换成完整 ToolExecution，保持状态序列和输出归档一致。
def _pre_handler_failure_execution(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime,
    decision: ActionDecision,
    progress: _AuthorizedExecutionProgress,
    outcome: ToolHandlerOutcome,
) -> ToolExecution:
    states = progress.states
    states.append("failed")
    projection = _project_output(request, call, runtime, outcome)
    result = _canonical_result(
        call,
        runtime,
        decision,
        outcome,
        projection,
        duration_ms=_elapsed_ms(progress.started_at),
        input_sources=progress.sources,
    )
    if result.failure_stage == ToolFailureStage.PERSISTENCE.value:
        states.append("persistence_failed")
    states.extend(("reconciled", "persisted", "projected"))
    return ToolExecution(call, decision, result, tuple(states))


def _normalized_call(
    request: ToolExecutorRequest,
    runtime: ToolRuntime,
) -> tuple[ToolCall, tuple[ToolInputSource, ...], str, dict[str, Any]]:
    internal = set(runtime.runtime_policy.input_policy.internal_parameters)
    forbidden = sorted(internal.intersection(request.call.arguments))
    if forbidden:
        return (
            request.call,
            (),
            "TOOL_INTERNAL_PARAMETER_FORBIDDEN",
            {"forbidden_fields": forbidden},
        )
    context = ToolInputCompletionContext(
        call_id=request.call.call_id,
        call_source=request.call.source_protocol,
        trusted_context=_trusted_context(request),
    )
    completion = complete_tool_arguments(request.call.arguments, runtime, context)
    normalized_arguments = canonicalize_owner_home_arguments(
        request.call.tool_name,
        completion.value,
        request.write_boundary,
    )
    call = replace(request.call, arguments=normalized_arguments)
    if completion.error_code:
        details: dict[str, Any] = {}
        if completion.conflict_fields:
            details["conflict_fields"] = list(completion.conflict_fields)
        return call, completion.sources, completion.error_code, details
    effect = request.business_effect
    if effect is not None:
        if (not isinstance(effect, BusinessEffectIdentity)
                or effect.owner_id != request.operation_owner_id or effect.task_id != _task_id(request)
                or runtime.runtime_policy.idempotency_policy.scope != 'operation'):
            return call, completion.sources, 'TOOL_AUTHORITY_CONTEXT_MISSING', {}
        return replace(call, operation_id=effect.operation_id, idempotency_key=effect.key), completion.sources, '', {}
    scope = runtime.runtime_policy.idempotency_policy.scope
    if scope != "business":
        return call, completion.sources, "", {}
    try:
        key = str(
            runtime.handler.business_idempotency_key(_handler_arguments(request, call, runtime))
            or ""
        ).strip()
    except Exception:
        key = ""
    if not key:
        return call, completion.sources, "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING", {}
    return replace(call, idempotency_key=key), completion.sources, "", {}


def _trusted_context(request: ToolExecutorRequest) -> dict[str, Any]:
    root = request.workspace_root.resolve(strict=False)
    effective_cwd = effective_registry_cwd(root, request.write_boundary)
    run_scope: dict[str, Any] = {
        "run_id": request.call.run_id,
        "attempt_id": request.call.attempt_id,
        "turn_id": request.call.turn_id,
    }
    trusted = request.trusted_run_context
    if isinstance(trusted, dict) and isinstance(trusted.get("run_scope"), dict):
        for key, value in trusted["run_scope"].items():
            if key not in {"run_id", "attempt_id", "turn_id"}:
                run_scope[str(key)] = value
    if isinstance(trusted, dict) and isinstance(trusted.get("task_attributes"), dict):
        run_scope["task_attributes"] = dict(trusted["task_attributes"])
    return {
        "run_scope": run_scope,
        "write_boundary": dict(request.write_boundary or {}),
        "registry": {
            "workspace_root": str(root),
            "effective_cwd": str(effective_cwd),
        },
    }


def _permitted_discovery_next_action(snapshot: ToolRuntimeSnapshot, target: str = "") -> str:
    from .registry_auth import snapshot_search_can_load

    if "tool_search" in snapshot.available_tool_names:
        if snapshot_search_can_load(snapshot):
            if target:
                return (
                    f'请先调用 tool_search(names=["{target}"]) 或 tool_search(action="catalog") '
                    "核实当前已授权工具与动作后再调用；禁止原样盲目重试。"
                )
            return '请先调用 tool_search(action="catalog") 或按能力查询已授权工具；禁止原样盲目重试。'
        return '请先调用 tool_search(action="catalog") 查看本人可见目录；若缺少权限请提交 capability_request，禁止原样盲目重试。'
    return "当前执行快照未提供发现入口，请核对已授权工具列表或提交 capability_request，禁止原样盲目重试。"


def _missing_snapshot_runtime_decision(
    snapshot: ToolRuntimeSnapshot,
    tool_name: str,
) -> ActionDecision:
    failures = {u_name: (u_code, u_reason) for u_name, u_code, u_reason in snapshot.unavailable_tools}
    if tool_name in failures:
        u_code, u_reason = failures[tool_name]
        code_text = str(u_code or "TOOL_UNAVAILABLE").strip() or "TOOL_UNAVAILABLE"
        reason_text = redact_sensitive_text(str(u_reason or "工具当前不可用")[:500])
        return ActionDecision(
            "deny",
            ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",),
            {
                "failure_stage": "authorization",
                "reported_error_code": code_text,
                "unavailable_reason": reason_text,
                "error_details": {
                    "tool_name": tool_name,
                    "unavailable_code": code_text,
                    "reason": reason_text,
                },
                "next_action": _permitted_discovery_next_action(snapshot),
            },
        )
    suggestions = difflib.get_close_matches(
        str(tool_name or ""),
        sorted(snapshot.available_tool_names),
        n=3,
        cutoff=0.6,
    )
    error_details: dict[str, object] = {"tool_name": str(tool_name or "")}
    if suggestions:
        error_details["suggestions"] = suggestions
    return ActionDecision(
        "deny",
        ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",),
        {
            "failure_stage": "authorization",
            "error_details": error_details,
            **({"suggestions": suggestions} if suggestions else {}),
            "next_action": _permitted_discovery_next_action(snapshot),
        },
    )


def _retired_alias_target_denial(
    snapshot: ToolRuntimeSnapshot,
    *,
    requested_name: str,
    target: str,
    action: str,
) -> ActionDecision | None:
    target_rt = snapshot.runtime(target)
    if target_rt is not None:
        action_prop = (
            target_rt.model_spec.input_schema.get("properties", {}).get("action", {})
            if isinstance(target_rt.model_spec.input_schema, dict)
            else {}
        )
        allowed_actions = action_prop.get("enum") if isinstance(action_prop, dict) else None
        if not isinstance(allowed_actions, list) or action in allowed_actions:
            return None
        reason = f"目标工具 {target} 当前未开放动作 {action}（受动作权限上界或服务就绪范围限制）"
        return ActionDecision(
            "deny",
            ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",),
            {
                "failure_stage": "authorization",
                "reported_error_code": "TOOL_ACTION_NOT_ALLOWED",
                "unavailable_reason": reason,
                "error_details": {
                    "requested_tool_name": requested_name,
                    "canonical_tool_name": target,
                    "action": action,
                    "allowed_actions": list(allowed_actions[:32]),
                },
                "next_action": _permitted_discovery_next_action(snapshot, target),
            },
        )
    failures = {u_name: (u_code, u_reason) for u_name, u_code, u_reason in snapshot.unavailable_tools}
    if target in failures or requested_name in failures:
        u_code, u_reason = failures.get(target) or failures[requested_name]
        code_text = str(u_code or "TOOL_UNAVAILABLE").strip() or "TOOL_UNAVAILABLE"
        reason_text = redact_sensitive_text(str(u_reason or "目标服务当前未就绪")[:500])
        return ActionDecision(
            "deny",
            ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",),
            {
                "failure_stage": "authorization",
                "reported_error_code": code_text,
                "unavailable_reason": f"{target} ({action}) 不可用：{reason_text}",
                "error_details": {
                    "requested_tool_name": requested_name,
                    "canonical_tool_name": target,
                    "action": action,
                    "unavailable_code": code_text,
                    "reason": reason_text,
                },
                "next_action": _permitted_discovery_next_action(snapshot, target),
            },
        )
    return ActionDecision(
        "deny",
        ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",),
        {
            "failure_stage": "authorization",
            "reported_error_code": "TOOL_NOT_ALLOWED",
            "unavailable_reason": f"已退役别名 {requested_name} 对应的目标工具 {target} ({action}) 未在当前授权快照中就绪",
            "error_details": {
                "requested_tool_name": requested_name,
                "canonical_tool_name": target,
                "action": action,
            },
            "next_action": _permitted_discovery_next_action(snapshot, target),
        },
    )


def _sanitized_error_details(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    cleaned = {
        str(k): v
        for k, v in raw.items()
        if str(k) != "next_action"
    }
    return redact_sensitive_value(cleaned) if cleaned else None


# LLM: 拒绝/待审批只归一宿主裁决结果，不调用handler；门决定专键来自ActionDecision供原归档写账。
# 函数用途: 生成执行前失败或待审批的标准结果，保留原因码与宿主证据。
def _decision_result(
    call: ToolCall,
    decision: ActionDecision,
    started_at: float,
    *,
    input_sources: tuple[ToolInputSource, ...] = (),
) -> ToolResult:
    code = decision.reason_codes[0] if decision.reason_codes else "RUNTIME_GATE_DENIED"
    stage = str(decision.evidence.get("failure_stage") or "authorization")
    status = "approval_required" if decision.status == "ask" else "failed"
    metadata: dict[str, object] = {"action_decision": decision.to_dict(), "input_sources": _source_dicts(input_sources),
                                   "plugin_gate_decisions": decision.evidence.get(PLUGIN_GATE_DECISION_EVIDENCE_KEY) or []}
    # 复核给出的具体原因码只进 reported_error_code；error_code 仍是分类表里的通用码，恢复逻辑不会遇到未登记码。
    reported = str(decision.evidence.get("reported_error_code") or "").strip().upper()
    if reported:
        metadata["reported_error_code"] = reported
    err_details = _sanitized_error_details(decision.evidence.get("error_details"))
    if err_details is None and isinstance(decision.evidence.get("issues"), list) and decision.evidence["issues"]:
        err_details = {"issues": decision.evidence["issues"][:8]}
        if isinstance(decision.evidence.get("allowed_parameters"), list):
            err_details["allowed_parameters"] = list(decision.evidence["allowed_parameters"][:32])
    if err_details is not None:
        metadata["error_details"] = err_details
    raw_next = decision.evidence.get("next_action")
    if isinstance(raw_next, str) and raw_next.strip():
        metadata["next_action"] = redact_sensitive_text(raw_next.strip())[:2048]
    return ToolResult.failed(
        call,
        _decision_message(decision),
        error_code=code,
        failure_stage=stage,
        facts=ToolFailureFacts(duration_ms=_elapsed_ms(started_at), status=status, metadata=metadata),
    )


# LLM: 只调用 handler 的可选 precheck_availability（没有就跳过：内置工具零成本、零 I/O）；异常按不可用处理；
# 不构造连接、不启动进程、不写状态，也不改变冻结快照或其哈希。
# 函数用途: 在弹审批前和批准后执行前复核代理工具是否还能执行，返回 None 表示放行。
def _pre_execution_precheck(runtime: ToolRuntime) -> ToolAvailability | None:
    method = getattr(runtime.handler, "precheck_availability", None)
    if not callable(method):
        return None
    try:
        availability = method()
    except Exception as exc:
        return ToolAvailability.unavailable(f"precheck failed: {type(exc).__name__}")
    if not isinstance(availability, ToolAvailability) or availability.available:
        return None
    return availability


# LLM: 复核失败统一按 TOOL_UNAVAILABLE 拒绝（不可重试、建议申请能力），具体原因码只进 reported_error_code；
# 不带原 ask 裁决的 gate 证据，只保留 approval_applied 标记与 resolved_effect，让渲染层能区分"审批前"与"批准后"。
# 函数用途: 把一次复核不可用的事实变成结构化拒绝裁决。
def _precheck_denial(decision: ActionDecision, availability: ToolAvailability, precheck: str) -> ActionDecision:
    evidence: dict[str, object] = {
        "failure_stage": "runtime_gate", "precheck": precheck, "reason": availability.reason,
        "reported_error_code": availability.error_code or "TOOL_UNAVAILABLE",
    }
    if decision.evidence.get("approval_applied") is True:
        evidence["approval_applied"] = True
    return ActionDecision("deny", ("TOOL_UNAVAILABLE",), evidence, resolved_effect=decision.resolved_effect)


# 函数用途: ask 裁决在弹审批框之前复核一次，代理已失效就直接拒绝，不写审批事件。
def _precheck_before_approval(runtime: ToolRuntime, decision: ActionDecision) -> ActionDecision:
    availability = _pre_execution_precheck(runtime)
    return decision if availability is None else _precheck_denial(decision, availability, "pre_approval")


# LLM: 敏感输入脱敏后有界截断（<= 16KiB），防超大输入撑爆操作记录；不修改原调用参数。
# 函数用途: 生成用于持久化操作回执的有界脱敏参数字典。
def _bound_redacted_input(arguments: dict[str, Any], max_bytes: int = 16384) -> dict[str, Any]:
    redacted = redact_sensitive_value(arguments) if isinstance(arguments, dict) else {}
    if not isinstance(redacted, dict):
        return {}
    try:
        encoded = json.dumps(redacted, ensure_ascii=False).encode('utf-8')
        if len(encoded) <= max_bytes:
            return redacted
        bounded: dict[str, Any] = {}
        for k, v in redacted.items():
            val_bytes = json.dumps(v, ensure_ascii=False).encode('utf-8')
            if len(val_bytes) > 2048:
                bounded[k] = f"<truncated:{len(val_bytes)} bytes>"
            else:
                bounded[k] = v
            if len(json.dumps(bounded, ensure_ascii=False).encode('utf-8')) > max_bytes - 128:
                bounded[k] = "<truncated>"
                break
        return bounded
    except Exception:
        return {}


_TRANSIENT_READ_RETRY_CODES = frozenset(
    {
        "TOOL_TIMEOUT",
        "HISTORY_STORAGE_BUSY",
        "RUNTIME_DB_RECOVERY_DEFERRED",
        "SCHEDULER_STORE_BUSY",
        "STORE_UNAVAILABLE",
    }
)
_TRANSIENT_ERRNOS = frozenset(
    code
    for code in (
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EINTR,
        errno.EBUSY,
        errno.ETIMEDOUT,
        getattr(errno, "ECONNRESET", None),
    )
    if isinstance(code, int)
)


def _can_auto_retry_read_only(request: ToolExecutorRequest, outcome: ToolHandlerOutcome) -> bool:
    if (
        outcome.ok
        or not outcome.retryable
        or not outcome.handler_executed
        or outcome.effect_outcome == "unknown"
        or _cancelled(request)
        or outcome.error_code == "CANCELLED"
    ):
        return False
    if outcome.failure_stage in {
        ToolFailureStage.VALIDATION.value,
        ToolFailureStage.AUTHORIZATION.value,
        ToolFailureStage.PROTOCOL.value,
        ToolFailureStage.RUNTIME_GATE.value,
    }:
        return False
    contract = error_contract(outcome.error_code)
    if not contract.retryable or contract.recommended_action not in {"retry", "retry_after_backoff"}:
        return False
    if outcome.error_code in _TRANSIENT_READ_RETRY_CODES:
        return True
    if contract.recommended_action == "retry_after_backoff" and contract.category not in {
        "validation",
        "permission",
        "authorization",
        "security",
        "protocol",
    }:
        return True
    if contract.category in {"transient", "timeout", "network"}:
        return True
    diag = outcome.result_envelope.get("diagnostic")
    if isinstance(diag, dict):
        if diag.get("category") in {"transient", "timeout"}:
            return True
        diag_errno = diag.get("errno")
        if type(diag_errno) is int and diag_errno in _TRANSIENT_ERRNOS:
            return True
    storage_err = outcome.result_envelope.get("storage_error")
    if isinstance(storage_err, dict) and storage_err.get("category") == "busy":
        return True
    return False


def _operation_identity(call: ToolCall, runtime: ToolRuntime | None) -> str:
    if runtime is None:
        return call.tool_name
    method = getattr(runtime.handler, "operation_identity_for", None)
    if callable(method):
        resolved = str(method(call.arguments) or "").strip()
        if resolved:
            return resolved
    return call.tool_name


# LLM: 执行仍沿原操作协调器写账；迁移来源须在handler结果持久化前附入信封，不能事后装饰审计。
# 函数用途: 按原读写及幂等策略执行一次已授权工具并保留可信迁移事实。
def _invoke_with_operation_policy(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime,
    decision: ActionDecision,
    started_at: float | None = None,
) -> ToolHandlerOutcome:
    if started_at is None:
        started_at = time.monotonic()
    redacted_input = _bound_redacted_input(call.arguments)
    op_identity = _operation_identity(call, runtime)

    # LLM: 只调用本次冻结的原handler；停止先拒绝，可信迁移信息不进入模型参数。
    # 函数用途: 执行或拒绝当前调用，在原操作账写结果前附上迁移来源。
    def invoke() -> ToolHandlerOutcome:
        if _cancelled(request):
            return ToolHandlerOutcome(
                op_identity if decision.resolved_effect != "read_only" else call.tool_name,
                False,
                "tool cancelled before handler entry",
                error_code="CANCELLED",
                failure_stage=ToolFailureStage.RUNTIME_GATE.value,
                handler_executed=False,
                duration_ms=max(0, _elapsed_ms(started_at)),
                effect_outcome="not_started",
            )
        outcome = invoke_registry_tool(
            _invoke_request(
                request,
                call,
                operation_managed=decision.resolved_effect != "read_only",
            )
        )
        outcome.duration_ms = max(0, _elapsed_ms(started_at))
        if decision.resolved_effect != "read_only":
            outcome.tool = op_identity
        if call.input_migration:
            outcome.result_envelope["input_migration"] = dict(call.input_migration)
        return outcome

    blocked = _require_operation_authority(request, call)
    if blocked is not None:
        return blocked
    if decision.resolved_effect == "read_only":
        if request.business_effect is not None:
            return ToolHandlerOutcome(call.tool_name, False,
                'business_effect仅用于有原操作账的写工具；只读工具无需业务项幂等，请删除该配置。',
                error_code='TOOL_INVALID_ARGUMENTS', effect_outcome='not_started',
                handler_executed=False, failure_stage=ToolFailureStage.VALIDATION.value)
        outcome = invoke()
        if _can_auto_retry_read_only(request, outcome):
            outcome = invoke()
            outcome.result_envelope.setdefault("tool_resilience", {})["retry_attempts"] = 1
        return outcome
    # seq 269 #3：mutating 路径 claim 前解析资源 scope，hook 故障 fail-closed
    # （TOOL_RESOURCE_SCOPE_RESOLUTION_FAILED，handler 不执行），绝不静默
    # 降级成无保护执行；read_only 路径无副作用不取锁，不受影响。
    try:
        resource_scopes = _durable_operation_scopes(
            _workspace_operation_scopes(request, call, runtime)
        )
    except ResourceScopeResolutionError as exc:
        return apply_tool_execution_facts(
            ToolHandlerOutcome(
                call.tool_name,
                False,
                f"资源 scope 解析失败（fail-closed，拒绝执行）: {exc}",
                error_code="TOOL_RESOURCE_SCOPE_RESOLUTION_FAILED",
                failure_stage=ToolFailureStage.RUNTIME_GATE.value,
                handler_executed=False,
            ),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
    return execute_tool_operation(
        ToolOperationExecutionRequest(
            store=request.operation_store,
            store_required=request.operation_store_required,
            owner_id=request.operation_owner_id,
            run_id=call.run_id,
            task_id=_task_id(request),
            operation_id=call.operation_id,
            tool_name=op_identity,
            args_hash=call.args_hash,
            idempotency_key=call.idempotency_key,
            idempotency_scope='business' if request.business_effect is not None else runtime.runtime_policy.idempotency_policy.scope,
            idempotency_namespace=op_identity,
            timeout_seconds=runtime.runtime_policy.timeout_policy.seconds,
            invoke=invoke,
            reconcile=None if request.business_effect is not None else _operation_reconciler(runtime, call),
            on_settled=_operation_settlement_notifier(runtime, call),
            resource_scopes=resource_scopes,
            attempt_id=call.attempt_id,
            redacted_input=redacted_input,
            settled_effect_outcome="none" if decision.resolved_effect == "read_only" else "committed",
        )
    )


# LLM: 冻结本调用的精确 task/run/attempt，回调只复读原权威，不重复审批、预算或 operation claim。
# 函数用途: 给需要锁内准入复查的长期资源提供只读权限检查，后续恢复轮不能替旧调用授权。
def _operation_authority_check(
    request: ToolExecutorRequest,
    call: ToolCall,
) -> Callable[[], None] | None:
    require = getattr(request.operation_store, "require_authority", None)
    if require is None:
        return None
    runtime = request.runtime_snapshot.runtime(call.tool_name)
    authority = ToolOperationAuthorityRequest(
        owner_id=request.operation_owner_id,
        run_id=call.run_id,
        task_id=_task_id(request),
        operation_id=call.operation_id,
        tool_name=_operation_identity(call, runtime),
        attempt_id=call.attempt_id,
    )
    return partial(require, authority)


# LLM: handler 前仍使用原结构化拒绝合同；长期资源锁内复查与这里共用同一只读权威检查。
# 函数用途: 在派发前确认原执行轮有效，缺失或故障均返回未进入 handler 的失败事实。
def _require_operation_authority(
    request: ToolExecutorRequest,
    call: ToolCall,
) -> ToolHandlerOutcome | None:
    """MANAGED 权威门（a1/a2/a3/l）：所有工具（含 read-only）handler 前过门。

    store 提供 require_authority（duck-typing，LocalStore 无 → 无门）：
    - 缺 repo / run 未登记 / current attempt 空 → AuthorityContextMissing →
      统一 TOOL_AUTHORITY_CONTEXT_MISSING + handler=0（fail-closed，不懒建链）。
    - 门自身故障（其它异常）→ TOOL_OPERATION_STORE_UNAVAILABLE（同族 fail-closed）。
    read-only 只跳过副作用 operation，不绕过 authority（l 契约）。
    """
    check = _operation_authority_check(request, call)
    if check is None:
        return None
    try:
        # 只在 handler 之前重读同一不可变身份，保留三次读取合同，合计 180ms 退避。
        # LOCKED 须先结束同连接冲突，结构损坏须停机；两者都不在这里盲重试。
        for attempt in range(3):
            try:
                check()
                break
            except Exception as exc:
                if sqlite_error_details(exc).get("category") != "busy" or attempt == 2:
                    raise
                time.sleep((0.060, 0.120)[attempt])
        return None
    except AuthorityContextMissing as exc:
        return apply_tool_execution_facts(
            ToolHandlerOutcome(
                call.tool_name,
                False,
                str(exc),
                error_code="TOOL_AUTHORITY_CONTEXT_MISSING",
                failure_stage=ToolFailureStage.RUNTIME_GATE.value,
                handler_executed=False,
            ),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
    except Exception as exc:  # noqa: BLE001 - 权威门故障 fail-closed
        return apply_tool_execution_facts(
            ToolHandlerOutcome(
                call.tool_name,
                False,
                "权威门故障，无法验证执行授权（fail-closed，拒绝执行）",
                error_code="TOOL_OPERATION_STORE_UNAVAILABLE",
                failure_stage=ToolFailureStage.RUNTIME_GATE.value,
                handler_executed=False,
                result_envelope={"storage_error": sqlite_error_details(exc)},
            ),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )


def _workspace_operation_scopes(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime,
) -> tuple[str, ...]:
    """workspace/逻辑资源 scope 解析（seq 245 P5 单一权威 resolver 薄包装）。

    唯一权威实现在 tooling/workspace_scopes.py；ActionPolicy（审计记录）与
    concurrency（调度投影）共用同一解析结果，绝不各算一套。executor 层必有
    workspace_root → 恒走 canonical 物理根归一化；持久 operation 只在调用点保留非 workspace scope。
    """
    from .workspace_scopes import authoritative_workspace_scopes

    scopes = authoritative_workspace_scopes(
        workspace_root=request.workspace_root,
        write_boundary=request.write_boundary,
        policy=runtime.runtime_policy,
        arguments=call.arguments,
    )
    # seq 253 #5：执行写根由工具结构化声明（BaseTool.effective_write_roots 协议），
    # 审计/调度 scope 与写边界共用同一提取器——替换按 internal_parameter 名字
    # （__sandbox_write_roots）的特判：特判只能覆盖声明过该参数名的工具，
    # 覆盖不了 apply_patch（写根在 patch 文本里）与 controlled_exec（写根在
    # grant.path_scope）这类执行写根不在参数里的工具。
    handler = getattr(runtime, "handler", None)
    extractor = getattr(handler, "effective_write_roots", None)
    if extractor is not None and request.workspace_root is not None:
        try:
            for root in extractor(
                call.arguments, request.write_boundary, request.workspace_root
            ):
                scopes = scopes + (f"workspace:{root}",)
        except ResourceScopeResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - scope failure fail-closed
            tool_identifier = getattr(call, "tool_name", None) or getattr(call, "name", "tool")
            raise ResourceScopeResolutionError(
                f"{tool_identifier}: write scope resolution failed: {type(exc).__name__}"
            ) from exc
    # seq 266 #3：运行期才可确定的资源（cancel 的 root 子树/status 展开目标、
    # audit 的当前 Audit、dispatch 的自动选池）由工具结构化声明
    # （BaseTool.effective_resource_scopes 协议，BaseTool 默认 ()）在 claim 前
    # 合并——参数投影覆盖不到的 scope 在此补齐；hook 内部解析失败必须自己
    # 保守锁稳定域（不空锁）。seq 269 #3：hook 抛错 = 实现缺陷，转
    # ResourceScopeResolutionError 向上传播，由调用方 fail-closed 拒绝执行，
    # 绝不静默降级成无保护执行（mutating handler 无锁执行 = 并发安全漏洞）。
    resource_extractor = getattr(handler, "effective_resource_scopes", None)
    if resource_extractor is not None:
        try:
            for scope in resource_extractor(
                call.arguments, request.write_boundary, request.workspace_root
            ):
                if str(scope or "").strip():
                    scopes = scopes + (str(scope).strip(),)
        except Exception as exc:  # noqa: BLE001 - hook 故障 fail-closed
            raise ResourceScopeResolutionError(
                f"{type(handler).__name__}.effective_resource_scopes failed: {exc}"
            ) from exc
    return tuple(dict.fromkeys(scopes))


# LLM: 会话运行时 tool concurrency keeps filesystem cwd/path scopes in the current turn's
# scheduler and sandbox, not in cross-run durable leases. Preserve non-filesystem logical scopes
# because they protect exact control-plane identities such as one agent run or watch subscription.
# 函数用途: 把工具解析出的范围缩成真正需要跨回合持久互斥的逻辑资源，普通文件路径只留作审计与沙箱事实。
def _durable_operation_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(scope).strip()
            for scope in scopes
            if str(scope or "").strip()
            and not str(scope).strip().startswith("workspace:")
        )
    )


# LLM: handler读取原快照与owner墙；独立owner只取宿主顶层身份，嵌套旧run_scope仍由原入口规范化，不授身份断言权。
# 函数用途: 把已通过权限门的调用和是否进入副作用账本的事实转成不可变 registry 执行请求。
def _human_approval_check(request, call):
    """仅原执行器闭包持有当前规范调用与批准记录，模型参数不能提供此能力。"""
    from ..contracts.gates.tool_approval_binding import (
        ApprovalBindingFacts,
        has_current_human_approval,
    )
    runtime = request.runtime_snapshot.runtime(call.tool_name)
    if runtime is None or not runtime.runtime_policy.approval_policy.require_human:
        return None
    def check():
        if _cancelled(request):
            raise PermissionError("HUMAN_RESPONSE_REQUIRED")
        boundary = request.write_boundary or {}
        actions = boundary.get('approved_actions', ())
        facts = ApprovalBindingFacts(tool_name=call.tool_name, run_id=call.run_id,
            operation_id=call.operation_id, idempotency_key=call.idempotency_key, args_hash=call.args_hash,
            approved_actions=tuple(actions) if isinstance(actions, (list, tuple)) else (),
            owner_id=request.operation_owner_id)
        if not has_current_human_approval(facts, time.time()):
            raise PermissionError('HUMAN_RESPONSE_REQUIRED')
        authority = _operation_authority_check(request, call)
        if authority is not None:
            authority()
    return check


def _invoke_request(
    request: ToolExecutorRequest,
    call: ToolCall,
    *,
    operation_managed: bool,
) -> RegistryToolInvokeRequest:
    tools = {
        runtime.model_spec.name: runtime.handler for runtime in request.runtime_snapshot.runtimes
    }
    runtime = request.runtime_snapshot.runtime(call.tool_name)
    payload = _handler_arguments(
        request,
        call,
        runtime,
        operation_managed=operation_managed,
    )
    trusted = request.trusted_run_context or {}
    owner_id = trusted.get("owner_id", "")
    return RegistryToolInvokeRequest(
        tool_name=call.tool_name,
        arguments=payload,
        tools=tools,
        workspace_root=request.workspace_root,
        workspace_roots=list(request.workspace_roots) or [request.workspace_root],
        allowed_tools=list(request.runtime_snapshot.available_tool_names),
        write_boundary=request.write_boundary,
        path_access_mode=request.path_access_mode,
        path_dangerous_roots=list(request.path_dangerous_roots),
        owner_scope_root=request.owner_scope_root,
        runtime_snapshot=request.runtime_snapshot,
        owner_type=request.owner_type,
        runtime=runtime,
        cancellation_token=request.cancellation_token,
        pending_input_probe=request.pending_input_probe,
        execution_authority_check=_operation_authority_check(request, call),
        tool_dispatch=_nested_dispatch(request, call),
        human_approval_check=_human_approval_check(request, call),
        on_handler_started=request.on_handler_started,
        owner_id=str(owner_id or ""),
        registry_owner_id=request.operation_owner_id,
    )


# LLM: 模型参数保持不变，只向声明的internal参数注入宿主事实；输入迁移与操作账状态不能来自模型字段。
# 函数用途: 给工具实现补入 owner、调用身份、取消令牌和副作用账本接管状态等宿主字段。
def _handler_arguments(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime | None,
    *,
    operation_managed: bool | None = None,
) -> dict[str, object]:
    """Add host-only inputs without changing the canonical model arguments."""

    payload: dict[str, object] = dict(call.arguments)
    internal_parameters = set(
        runtime.runtime_policy.input_policy.internal_parameters if runtime is not None else ()
    )
    if "__run_scope" in internal_parameters:
        payload["__run_scope"] = _handler_run_scope(request, call)
    if "__tool_call_id" in internal_parameters:
        payload["__tool_call_id"] = call.call_id
    if "__operation_id" in internal_parameters:
        payload["__operation_id"] = call.operation_id
    if "__operation_managed" in internal_parameters and operation_managed is not None:
        payload["__operation_managed"] = operation_managed
    if "__input_migration" in internal_parameters and call.input_migration:
        payload["__input_migration"] = dict(call.input_migration)
    if "__cancellation_token" in internal_parameters:
        payload["__cancellation_token"] = request.cancellation_token
    if "__process_completion_target" in internal_parameters:
        trusted = request.trusted_run_context or {}
        payload["__process_completion_target"] = dict(trusted.get("process_completion_target") or {})
    if "__observation_screenshot" in internal_parameters:
        trusted = request.trusted_run_context or {}
        payload["__observation_screenshot"] = trusted.get("observation_screenshot") is True
    return payload


# LLM: 身份与 owner 地址只由宿主请求覆盖；canonical owner home 不随 Full Access 的路径墙清空，
# 不能接受模型参数或旧 run_scope 覆盖。修改时同步进程、PTY 与跨会话权限回归。
# 函数用途: 为工具冻结本轮真实身份和持久状态地址；此地址不授予额外文件访问权限。
def _handler_run_scope(
    request: ToolExecutorRequest,
    call: ToolCall,
) -> dict[str, object]:
    trusted = request.trusted_run_context
    supplied = (
        dict(trusted.get("run_scope") or {})
        if isinstance(trusted, dict) and isinstance(trusted.get("run_scope"), dict)
        else {}
    )
    supplied.update(
        {
            "owner_id": request.operation_owner_id,
            "owner_home": str((request.write_boundary or {}).get("canonical_owner_home_root")
                              or request.owner_scope_root or ""),
            "run_id": call.run_id,
            "task_id": _task_id(request),
            "attempt_id": call.attempt_id,
            "turn_id": call.turn_id,
        }
    )
    return supplied


def _operation_reconciler(
    runtime: ToolRuntime,
    call: ToolCall,
) -> Callable[[ToolOperationRecord], ToolOperationReconciliation]:
    # 原公共 DTO 由 W12 独占：旧合同经 prior_result 增量携带事实；公共字段
    # 已落地时同时填入同名字段，不造影子 DTO，也不以 handler_started_at 补时间。
    def reconcile(record: ToolOperationRecord) -> ToolOperationReconciliation:
        dispatch_facts = {
            "first_dispatch_at": record.first_dispatch_at,
            "component_receipts": deepcopy(record.component_receipts),
            "attempt_id": record.attempt_id,
        }
        optional = {item.name for item in fields(ToolOperationReconciliationContext)}
        return runtime.handler.reconcile_operation(
            call.arguments,
            ToolOperationReconciliationContext(
                owner_id=record.owner_id,
                run_id=record.run_id,
                task_id=record.task_id,
                operation_id=record.operation_id,
                tool_name=record.tool or call.tool_name,
                args_hash=record.args_hash,
                idempotency_key=record.idempotency_key or call.idempotency_key,
                idempotency_scope=(
                    record.idempotency_scope or runtime.runtime_policy.idempotency_policy.scope
                ),
                prior_result={**deepcopy(record.result or {}), **dispatch_facts},
                **{key: value for key, value in dispatch_facts.items() if key in optional},
            ),
        )

    return reconcile


# LLM: The coordinator owns when settlement is authoritative; this adapter only routes the typed
# notification back to the same immutable handler that executed/reconciled the operation.
# 函数用途: 把通用操作账本的终态通知转给具体工具，用于回收工具私有的临时恢复材料。
def _operation_settlement_notifier(
    runtime: ToolRuntime,
    call: ToolCall,
) -> Callable[[ToolOperationSettlementContext], None]:
    def notify(context: ToolOperationSettlementContext) -> None:
        hook = getattr(runtime.handler, "on_operation_settled", None)
        if callable(hook):
            hook(call.arguments, context)

    return notify


def _project_output(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime,
    outcome: ToolHandlerOutcome,
) -> ToolOutputProjection:
    """归档后只补安全诊断；失败不再写同一磁盘，不撤销或重放已执行 handler。"""
    if request.output_archiver is not None:
        try:
            return request.output_archiver(call, outcome)
        except Exception as exc:  # noqa: BLE001 - paired result must survive archiver failure
            raw = str(outcome.output or "")
            encoded = raw.encode("utf-8")
            return ToolOutputProjection(
                (),
                (),
                {
                    "projection_error_code": "TOOL_OUTPUT_ARCHIVE_FAILED",
                    "projection_exception_type": type(exc).__name__,
                    "raw_output_chars": len(raw),
                    "raw_output_bytes": len(encoded),
                    "raw_output_sha256": hashlib.sha256(encoded).hexdigest(),
                    **_archive_failure_diagnostic(request, call, exc),
                },
            )
    output_policy = output_policy_for_outcome(runtime.runtime_policy, outcome)
    output = project_tool_output_body(
        tool=call.tool_name,
        output=outcome.output,
        trust=output_policy.trust,
        redaction=output_policy.redaction,
    )
    raw = str(outcome.output or "")
    metadata = {
        "raw_output_chars": len(raw),
        "raw_output_bytes": len(raw.encode("utf-8")),
        "raw_output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }
    # The production Tool Gateway always supplies ``output_archiver``.  Direct
    # contract probes without one retain the full body here; silently clipping
    # it would lose data while falsely implying that a durable ref exists.
    refs = _result_refs(outcome)
    blocks: list[ToolContentBlock] = []
    if output:
        blocks.append(ToolContentBlock("text", text=output))
    blocks.extend(ToolContentBlock("ref", ref=item.ref) for item in refs)
    return ToolOutputProjection(tuple(blocks), refs, metadata)


def _archive_failure_diagnostic(request: ToolExecutorRequest, call: ToolCall, exc: Exception) -> dict[str, Any]:
    """只取原生 errno 和最多四层显式 cause；不序列化异常正文、文件名或工具正文。"""
    causes, seen, number = [], set(), None
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(causes) < 4:
        seen.add(id(current))
        native_errno = current.errno if isinstance(current, OSError) else None
        if type(native_errno) is not int or native_errno.bit_length() > 64:
            native_errno = None
        if number is None:
            number = native_errno
        causes.append({'error_type': type(current).__name__[:96], 'errno': native_errno})
        current = current.__cause__
    identity = (request.operation_owner_id, call.run_id, call.attempt_id, call.turn_id, call.call_id)
    reference = 'volatile:' + hashlib.sha256(json.dumps(identity).encode('utf-8')).hexdigest()
    contract = error_contract('TOOL_OUTPUT_ARCHIVE_FAILED')
    diagnostic = DiagnosticEnvelope(code=contract.code, category=contract.category,
        retryable=contract.retryable, errno=number, operation='tool_output.archive',
        diagnostic_ref=reference, details_truncated=current is not None).to_dict()
    return {'diagnostic': diagnostic, 'diagnostic_ref': reference, 'diagnostic_storage': 'volatile',
            'archive_diagnostic': {'causes': causes}, 'errno': number, 'operation': diagnostic['operation']}


# LLM: handler执行后沿原输出/操作事实归一结果；门决定只取宿主evidence覆盖同名metadata，不信handler授权字段。
# 函数用途: 将真实handler结果与宿主决定组装成可归档的标准工具结果。
def _canonical_result(
    call: ToolCall,
    runtime: ToolRuntime,
    decision: ActionDecision,
    outcome: ToolHandlerOutcome,
    projection: ToolOutputProjection,
    *,
    duration_ms: int,
    input_sources: tuple[ToolInputSource, ...],
) -> ToolResult:
    operation = _operation_from_outcome(call, outcome)
    effect_outcome = _effect_outcome(decision, outcome, operation)
    output_policy = output_policy_for_outcome(runtime.runtime_policy, outcome)
    metadata = {
        **dict(projection.metadata),
        "action_decision": decision.to_dict(),
        "input_sources": _source_dicts(input_sources),
        "reported_error_code": outcome.reported_error_code,
        "host_error": dict(outcome.host_error),
        **({"source_effect": dict(outcome.source_effect)} if outcome.source_effect else {}),
        "handler_details": dict(outcome.result_envelope or {}),
        "plugin_gate_decisions": decision.evidence.get(PLUGIN_GATE_DECISION_EVIDENCE_KEY) or [],
    }
    err_details = _sanitized_error_details((outcome.result_envelope or {}).get("error_details"))
    if err_details is None:
        err_details = _sanitized_error_details(decision.evidence.get("error_details"))
    if err_details is not None:
        metadata["error_details"] = err_details
    next_advice = str(outcome.next_action or "").strip()
    if not next_advice:
        ev_next = decision.evidence.get("next_action")
        if isinstance(ev_next, str) and ev_next.strip():
            next_advice = redact_sensitive_text(ev_next.strip())[:2048]
    if next_advice:
        metadata["next_action"] = next_advice
    if (
        operation is not None
        and operation.replayed
        and decision.resolved_effect != "read_only"
        and operation.status in {"failed", "cancelled", "succeeded"}
    ):
        metadata["replayed_terminal_mutation"] = True
    projection_error = str(projection.metadata.get("projection_error_code") or "")
    if projection_error:
        diagnostic = projection.metadata.get('diagnostic') or {}
        return ToolResult.failed(
            call,
            "The tool ran, but its output could not be archived safely. "
            f"errno={diagnostic.get('errno')}; diagnostic_ref={diagnostic.get('diagnostic_ref')}; storage=volatile",
            error_code=projection_error,
            failure_stage=ToolFailureStage.PERSISTENCE.value,
            facts=ToolFailureFacts(
                handler_executed=outcome.handler_executed,
                duration_ms=duration_ms,
                operation=operation,
                effect_outcome=effect_outcome,
                effect_source_ref=(
                    outcome.effect_source_ref
                    or (operation.effect_source_ref if operation is not None else "")
                ),
                refs=(),
                output_trust=output_policy.trust,
                output_redaction=output_policy.redaction,
                metadata=metadata,
                status="unknown" if effect_outcome == "unknown" else "failed",
            ),
        )
    if outcome.ok:
        return ToolResult.succeeded(
            call,
            facts=ToolSuccessFacts(
                content_blocks=projection.content_blocks,
                operation=operation,
                effect_outcome=effect_outcome,
                effect_source_ref=(
                    outcome.effect_source_ref
                    or (operation.effect_source_ref if operation is not None else "")
                ),
                refs=projection.refs,
                output_trust=output_policy.trust,
                output_redaction=output_policy.redaction,
                metadata=metadata,
                handler_executed=outcome.handler_executed,
                duration_ms=duration_ms,
            ),
        )
    status = "cancelled" if outcome.error_code == "CANCELLED" else "failed"
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=status,
        content_blocks=projection.content_blocks,
        error_code=outcome.error_code or "UNKNOWN_ERROR",
        handler_executed=outcome.handler_executed,
        failure_stage=(
            outcome.failure_stage
            or (
                ToolFailureStage.EXECUTION.value
                if outcome.handler_executed
                else ToolFailureStage.RUNTIME_GATE.value
            )
        ),
        duration_ms=duration_ms,
        operation=operation,
        effect_outcome=effect_outcome,
        effect_source_ref=(
            outcome.effect_source_ref
            or (operation.effect_source_ref if operation is not None else "")
        ),
        refs=projection.refs,
        output_trust=output_policy.trust,
        output_redaction=output_policy.redaction,
        metadata=metadata,
    )


def _operation_from_outcome(
    call: ToolCall,
    outcome: ToolHandlerOutcome,
) -> ToolOperation | None:
    value = outcome.result_envelope.get("tool_operation")
    if not isinstance(value, dict):
        return None
    operation_id = str(value.get("operation_id") or call.operation_id)
    status = str(value.get("status") or "unknown")
    source_ref = str(
        outcome.effect_source_ref
        or value.get("reconciliation_source_ref")
        or (f"tool-operation://{call.run_id}/{operation_id}" if status == "succeeded" else "")
    )
    return ToolOperation(
        operation_id=operation_id,
        idempotency_key=call.idempotency_key,
        args_hash=call.args_hash,
        status=status,
        handler_executed=outcome.handler_executed,
        effect_outcome=str(
            outcome.effect_outcome
            or _effect_from_operation_status(value, handler_executed=outcome.handler_executed)
        ),
        effect_source_ref=source_ref,
        attempt_count=max(1, int(value.get("attempt_count") or 1)),
        result_ref=str(value.get("result_ref") or ""),
        replayed=value.get("replayed") is True,
    )


def _effect_outcome(
    decision: ActionDecision,
    outcome: ToolHandlerOutcome,
    operation: ToolOperation | None,
) -> str:
    if decision.resolved_effect == "read_only":
        return "confirmed" if outcome.ok else (outcome.effect_outcome or "not_started")
    if operation is not None:
        return operation.effect_outcome
    if outcome.ok:
        return "confirmed"
    if outcome.effect_outcome:
        return outcome.effect_outcome
    return "unknown" if outcome.handler_executed else "not_started"


def _effect_from_operation_status(
    value: dict[str, Any],
    *,
    handler_executed: bool | None = None,
) -> str:
    explicit = str(value.get("effect_outcome") or "").strip().lower()
    if explicit in {"confirmed", "not_started", "failed", "unknown"}:
        return explicit
    status = str(value.get("status") or "").strip().lower()
    if status == "succeeded":
        return "confirmed"
    if status == "not_started":
        return "not_started"
    orig_exec = value.get("original_tool_execution")
    if isinstance(orig_exec, dict) and orig_exec.get("handler_executed") is False:
        return "not_started"
    if handler_executed is False and value.get("replayed") is not True:
        return "not_started"
    return "unknown"


def _result_refs(outcome: ToolHandlerOutcome) -> tuple[ToolResultRef, ...]:
    candidates: list[object] = []
    for key in ("tool_result_refs", "artifact_refs", "output_refs"):
        value = outcome.result_envelope.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    refs: list[ToolResultRef] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or item.get("path") or item.get("uri") or "").strip()
        if not ref:
            continue
        try:
            refs.append(
                ToolResultRef(
                    kind=str(item.get("kind") or "artifact"),
                    ref=ref,
                    sha256=str(item.get("sha256") or ""),
                    size_bytes=int(item.get("size_bytes") or 0),
                    summary=str(item.get("summary") or ""),
                    mime_type=str(item.get("mime_type") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(refs)


# LLM: 只渲染 ActionPolicy 结构化裁决；重复门的有界恢复说明随原回执进入模型，参数值/密钥不回显。
# 函数用途: 让模型和用户知道哪里被拦及如何换路；不修改裁决，不替模型执行或自动重试。
def _decision_message(decision: ActionDecision) -> str:
    if decision.status == "ask":
        return "Approval or explicit user confirmation is required before this tool can run."
    message = "Tool execution was blocked before the handler ran: " + ", ".join(
        decision.reason_codes or ("RUNTIME_GATE_DENIED",)
    )
    if decision.evidence.get('business_scope_checked') is True:
        reason = decision.evidence.get('reported_error_code')
        if isinstance(reason, str) and reason:
            message += '。具体原因: ' + reason[:128]
            facts = {key: value[:512] for key in ('package_ref', 'expected_version_ref', 'actual_version_ref',
                'expected_activation_id', 'actual_activation_id', 'actual_state')
                if isinstance(value := decision.evidence.get(key), str)}
            if facts:
                message += '；' + json.dumps(facts, ensure_ascii=False)
            next_action = decision.evidence.get('next_action')
            if isinstance(next_action, str) and next_action:
                message += '。下一步: ' + next_action[:512]
    boundary_error = str(decision.evidence.get("boundary_error") or "").strip()
    if boundary_error:
        message += f". {boundary_error}"
    unavail_reason = str(decision.evidence.get("unavailable_reason") or "").strip()
    if unavail_reason:
        reported_code = str(decision.evidence.get("reported_error_code") or "").strip()
        prefix = f" ({reported_code})" if reported_code else ""
        message += f"。不可用原因{prefix}: {unavail_reason[:500]}"
    suggestions = decision.evidence.get("suggestions")
    if isinstance(suggestions, list) and suggestions and all(isinstance(s, str) for s in suggestions):
        message += "。当前快照最接近的已授权工具: " + ", ".join(s[:128] for s in suggestions[:3])
    err_details = decision.evidence.get("error_details")
    if isinstance(err_details, dict):
        forbidden_fields = err_details.get("forbidden_fields")
        if isinstance(forbidden_fields, list) and forbidden_fields:
            message += "。禁止传入内部字段: " + ", ".join(str(f)[:128] for f in forbidden_fields[:16])
        conflict_fields = err_details.get("conflict_fields")
        if isinstance(conflict_fields, list) and conflict_fields:
            message += "。与宿主可信上下文冲突的字段: " + ", ".join(str(f)[:128] for f in conflict_fields[:16])
    precheck = str(decision.evidence.get("precheck") or "")
    if precheck:
        when = "审批前" if precheck == "pre_approval" else "批准后、执行前"
        message += (f"。工具在{when}复核时已不可用：{decision.evidence.get('reason') or ''}。"
                    "本次未执行，也不会再等待审批；请换用其它可执行工具或申请能力。")
    rendered_issues = _render_validation_issues(decision.evidence.get("issues"))
    if rendered_issues:
        message += " 参数问题: " + rendered_issues
        allowed = decision.evidence.get("allowed_parameters")
        if isinstance(allowed, list) and all(isinstance(name, str) for name in allowed):
            message += "。该工具接受的参数: " + ", ".join(allowed)
    gate = decision.evidence.get("gate")
    if isinstance(gate, dict):
        message += _render_gate_findings(gate.get("findings"))
        if gate.get("gate") == "tool_guardrail" and gate.get("recommended_action") == "change_strategy":
            detail = gate.get("model_message")
            if isinstance(detail, str) and detail:
                message += "。重复诊断: " + detail[:500]
            message += "。本次工具未执行；请基于已有结果调整下一步，而不是原样重试。此拒绝不证明任务完成。"
    return message


# LLM: 只输出已判定 finding 中的字段名/命令名/规则名，不重新解析 shell、不输出 argv 或用户数据。
# 函数用途: 给拒绝提示补上具体命中的命令或规则，例如命令链中的 rm；安全门仍维持原判定。
def _render_gate_findings(raw: object) -> str:
    if not isinstance(raw, list):
        return ""
    details: list[str] = []
    for finding in raw[:4]:
        evidence = finding.get("evidence") if isinstance(finding, dict) else None
        if not isinstance(evidence, dict):
            continue
        for key, label in (("field", "参数"), ("executable", "命令"), ("pattern", "规则")):
            value = evidence.get(key)
            if isinstance(value, str) and value:
                details.append(f"{label}={json.dumps(value[:128], ensure_ascii=False)}")
    return "。拦截位置: " + "; ".join(details) if details else ""


# LLM: schema issue 是唯一诊断来源，不能靠模型错误文字识别参数；保留有界输出及不打印参数值的约束。
# 函数用途: 把参数缺失、额外字段或类型错误解释成可操作说明，供模型在下一次调用自行修正。
def _render_validation_issues(raw: object) -> str:
    """把 schema 校验的字段级诊断渲染成模型可读文本(recovery_hint 承诺的 名:期望类型)。

    实机教训(test2-req-7 urllib3 复刻):validation 拒绝只回错误码不带字段详情,
    MiniMax-M2.7 盲猜重试 send_message 57 轮全失败、任务零推进。这里只渲染结构化的
    path/keyword/expected/actual_type,绝不回显参数值(防凭据进模型/审计)。
    """
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        parts.append(_render_validation_issue(item))
        if len(parts) >= 4:
            break
    return "; ".join(parts)


# LLM: 单条 issue 独立渲染，主循环只做有界收集；分支只读取结构化诊断字段，绝不检查参数正文。
# 函数用途: 把一种参数校验问题转换成下一次调用可操作的安全提示。
def _render_validation_issue(item: dict[str, object]) -> str:
    path = str(item.get("path") or "").strip()
    keyword = str(item.get("keyword") or "").strip()
    details = item.get('details')
    facts = details if isinstance(details, dict) else {}
    expected_type = facts.get('expected_type')
    array_hint = '；请传 JSON 空数组 []' if facts.get('requires_empty_array') is True else ''
    if keyword == "required":
        typed = f'，期望 {expected_type}' if expected_type in ('array', 'object', 'string', 'number', 'integer', 'boolean', 'null') else ''
        return f"{path}: 必填缺失{typed}{array_hint}"
    if keyword == "additionalProperties":
        return _render_unknown_property_issue(path, item.get("details"))
    if keyword == "type":
        return f"{path}: 期望 {item.get('expected')}, 实际 {item.get('actual_type') or '?'}{array_hint}"
    if keyword in ('minLength', 'maxLength', 'minItems', 'maxItems', 'minProperties', 'maxProperties'):
        rendered = _render_length_boundary_issue(path, keyword, item.get('expected'), facts)
        if rendered:
            return rendered
    if keyword in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'):
        rendered = _render_numeric_boundary_issue(path, keyword, item.get('expected'), facts)
        if rendered:
            return rendered
    if keyword in ('oneOf', 'anyOf'):
        if facts.get('multiple_matches') is True:
            return f'{path}: 同时符合多个 {keyword} 分支，请按 schema 只保留目标分支独有的参数'
        return f'{path}: 没有符合 {keyword} 的参数组合，请对照工具 schema 选择分支并补齐该分支必填字段'
    if keyword == "exclusiveArguments":
        return _render_exclusive_argument_issue(path, item.get("details"))
    return f"{path}: {keyword} 校验失败"



def _safe_boundary_number(value: object) -> str | None:
    """只展示有界、有限的原结构化数；拒绝bool、字符串和伪造的超大整数。"""
    if type(value) is int and value.bit_length() <= 256:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return str(value)
    return None


def _render_length_boundary_issue(path: str, keyword: str, expected: object, facts: dict) -> str:
    """字符、数组项、对象字段分开计量；只读validator计数，不读取或裁剪原输入。"""
    if type(expected) is not int or expected < 0 or expected.bit_length() > 256:
        return ''
    kind, unit = {'Length': ('字符串', '个字符'), 'Items': ('数组', '项'),
                  'Properties': ('对象', '个字段')}[keyword[3:]]
    relation = '至少' if keyword.startswith('min') else '最多'
    count = facts.get('actual_length')
    actual = f'；实际 {count} {unit}' if type(count) is int and count >= 0 and count.bit_length() <= 256 else ''
    direction = '补齐' if keyword.startswith('min') else '缩减'
    unit_hint = '字符数不是UTF-8字节数。' if keyword.endswith('Length') else ''
    return (f'{path}: {keyword} 要求{kind}{relation} {expected} {unit}{actual}。'
            f'下一步：保留原需求，在原输入上{direction}此字段至该边界。'
            f'{unit_hint}本次未执行，勿原样重试。')


def _render_numeric_boundary_issue(path: str, keyword: str, expected: object, facts: dict) -> str:
    """没有结构化实际值就只展示边界，不从异常文字或原输入猜测数值。"""
    limit = _safe_boundary_number(expected)
    if limit is None:
        return ''
    relations = {'minimum': '>=', 'maximum': '<=', 'exclusiveMinimum': '>', 'exclusiveMaximum': '<'}
    requirement = f'数值必须是 {limit} 的整数倍' if keyword == 'multipleOf' else f'数值必须 {relations[keyword]} {limit}'
    actual = _safe_boundary_number(facts.get('actual_number'))
    suffix = f'；实际数值 {actual}' if actual is not None else ''
    return (f'{path}: {keyword} 要求{requirement}{suffix}。'
            '下一步：在原输入上按该数值关系修正此字段，仍传JSON数字。本次未执行，勿原样重试。')


def _render_unknown_property_issue(path, raw):
    """只消费校验器产生的路径、类型和有界允许字段，不回显原参数。"""
    message = f"{path}: 未声明字段(该工具不接受额外参数)"
    if not isinstance(raw, dict):
        return message
    names = raw.get('allowed_fields')
    if isinstance(names, list):
        names = [name[:128] for name in names[:16] if isinstance(name, str)]
        if names:
            message += '；当前层允许字段: ' + ', '.join(names)
    examples = {'boolean': 'true', 'integer': '0', 'number': '0', 'string': '"示例"',
                'object': '{}', 'array': '[]', 'null': 'null'}
    target, expected = raw.get('suggested_path'), raw.get('expected_type')
    if isinstance(target, str) and isinstance(expected, str) and expected in examples:
        message += f'；请移至 {target[:512]}，类型 {expected}，示例值 {examples[expected]}'
    else:
        message += '；删除额外字段，按工具声明的嵌套结构重新调用；无法确定正确层级时先核对工具参数'
    return message


# LLM: 独立渲染一条互斥问题可让主循环保持两层以内；输出只消费安全提取器认可的字段名。
# 函数用途: 把一组结构化冲突详情转换为模型可见的有界 JSON 文本。
def _render_exclusive_argument_issue(path: str, raw: object) -> str:
    details = _safe_conflict_details(raw)
    if not details:
        return f"{path}: exclusiveArguments 校验失败"
    return f"{path}: 互斥参数冲突 details=" + json.dumps(
        details,
        ensure_ascii=False,
    )


# LLM: 模型提示只允许输出 schema 中冻结的参数名列表，忽略 issue 里其它任意字段，防止参数值或伪造控制数据借 details 泄露。
# 函数用途: 从互斥 issue 中提取有界、可 JSON 展示的冲突参数名与完整参数组。
def _safe_conflict_details(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key in ("conflicting_arguments", "exclusive_group"):
        names = raw.get(key)
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            return {}
        result[key] = [name[:128] for name in names[:32]]
    return result


def _task_id(request: ToolExecutorRequest) -> str:
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    trusted = request.trusted_run_context
    run_scope = (
        trusted.get("run_scope")
        if isinstance(trusted, dict) and isinstance(trusted.get("run_scope"), dict)
        else {}
    )
    # run_id 不是 task 声明（seq 248 #4）：run_scope.task_id 兜底 run_id 会把
    # 测试/非主链调用伪装成 task；权威链上 task_id 为空时请求也应为空，
    # claim/authority 按「非空严格比对、空跳过」语义处理。
    return str(run_scope.get("task_id") or boundary.get("task_id") or "")


def _cancelled(request: ToolExecutorRequest) -> bool:
    return bool(request.cancellation_token and request.cancellation_token.cancelled)


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.monotonic() - started_at) * 1000))


def _outcome_state(outcome: ToolHandlerOutcome) -> str:
    if outcome.error_code == "CANCELLED":
        return "cancelled"
    if outcome.effect_outcome == "unknown":
        return "unknown"
    return "failed"


def _source_dicts(sources: tuple[ToolInputSource, ...]) -> list[dict[str, str]]:
    return [item.to_dict() for item in sources]


__all__ = [
    "ToolExecution",
    "ToolExecutor",
    "ToolExecutorRequest",
    "ToolOutputProjection",
]


# LLM: 只为有限程序本次构造派发器，内层再入时关闭，避免循环与权限放大；不执行工具。
# 函数用途: 为原调用上下文附加同身份的只读批次入口。
def _nested_dispatch(request, call):
    if not request.allow_nested_dispatch or call.tool_name != 'tool_program':
        return None
    from .nested_dispatch import NestedToolDispatcher
    return NestedToolDispatcher(request, call)
