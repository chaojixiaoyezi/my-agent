# LLM: 工具只通过本执行状态机进入 handler；错误展示读取结构化裁决，不扩大权限或自动修写参数。
# 模块用途: 统一工具校验、授权、执行和结果记录；拒绝时给出可定位原因，保证未执行与已执行明确区分。
from __future__ import annotations

"""The only state machine allowed to enter a registered tool handler."""

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

from ..local_storage import ToolOperationRecord
from ..runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ToolOperationAuthorityRequest,
)
from .action_policy import ActionDecision, ActionPolicy, ActionPolicyRequest
from .cancellation import CancellationToken
from .models import (
    ResourceScopeResolutionError,
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
from .registry_invoke import RegistryToolInvokeRequest, invoke_registry_tool
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


# LLM: 模型参数与宿主执行配置分离；approval_mode 是 owner 控制面快照，不能从 ToolCall 参数提权。
# 类用途: 汇总本次调用、权限、账本和取消上下文，供唯一执行器使用。
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


# LLM: Pre-handler exits reuse the authorized call's immutable input sources/start time and the
# mutable lifecycle trace. Bundling them prevents the failure helper from becoming a parallel API.
# 类用途: 保存一次已授权工具执行在进入 handler 前共用的计时、输入来源和状态轨迹。
@dataclass(frozen=True)
class _AuthorizedExecutionProgress:
    sources: tuple[ToolInputSource, ...]
    started_at: float
    states: list[str]


class ToolExecutor:
    def __init__(self, policy: ActionPolicy | None = None) -> None:
        self.policy = policy or ActionPolicy()

    def execute(self, request: ToolExecutorRequest) -> ToolExecution:
        started_at = time.monotonic()
        states = ["received"]
        runtime = request.runtime_snapshot.runtime(request.call.tool_name)
        if runtime is None:
            decision = ActionDecision("deny", ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",))
            result = _decision_result(request.call, decision, started_at)
            return ToolExecution(
                request.call,
                decision,
                result,
                (*states, "persisted", "projected"),
            )

        call, sources, normalization_error = _normalized_call(request, runtime)
        states.append("normalized")
        if normalization_error:
            _LOGGER.warning(
                "tool validation failed: tool=%s run=%s turn=%s error=%s "
                "arguments_head=%r",
                str(request.call.tool_name or ""),
                str(getattr(request.call, "run_id", "") or ""),
                str(getattr(request.call, "turn_id", "") or ""),
                normalization_error,
                json.dumps(request.call.arguments, ensure_ascii=False)[:500],
            )
            decision = ActionDecision(
                "deny",
                (normalization_error,),
                {"failure_stage": ToolFailureStage.VALIDATION.value},
            )
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
                required_action=request.required_action,
            )
        )
        states.extend(("validated", "authorized"))
        if not decision.allowed:
            states.append("approval_pending" if decision.status == "ask" else "failed")
            result = _decision_result(call, decision, started_at, input_sources=sources)
            return ToolExecution(call, decision, result, (*states, "persisted", "projected"))

        return _execute_authorized(
            request,
            call,
            runtime,
            decision,
            sources,
            started_at,
            states,
        )


def _execute_authorized(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime,
    decision: ActionDecision,
    sources: tuple[ToolInputSource, ...],
    started_at: float,
    states: list[str],
) -> ToolExecution:
    """Execute a call only after normalization and ActionPolicy authorization."""
    progress = _AuthorizedExecutionProgress(sources, started_at, states)
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
    outcome = _invoke_with_operation_policy(request, call, runtime, decision)
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
        outcome = ToolHandlerOutcome(
            call.tool_name,
            False,
            f"工具调用参数校验器故障，已在执行前拒绝：{type(exc).__name__}",
            error_code="TOOL_ERROR",
            reported_error_code="TOOL_INVOCATION_VALIDATOR_FAILED",
        )
    if outcome is None:
        return None
    if not isinstance(outcome, ToolHandlerOutcome) or outcome.ok:
        outcome = ToolHandlerOutcome(
            call.tool_name,
            False,
            "工具调用参数校验器返回了非法结果，已在执行前拒绝",
            error_code="TOOL_ERROR",
            reported_error_code="TOOL_INVOCATION_VALIDATOR_CONTRACT_BROKEN",
        )
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
) -> tuple[ToolCall, tuple[ToolInputSource, ...], str]:
    internal = set(runtime.runtime_policy.input_policy.internal_parameters)
    if internal.intersection(request.call.arguments):
        return request.call, (), "TOOL_INTERNAL_PARAMETER_FORBIDDEN"
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
        return call, completion.sources, completion.error_code
    scope = runtime.runtime_policy.idempotency_policy.scope
    if scope != "business":
        return call, completion.sources, ""
    try:
        key = str(
            runtime.handler.business_idempotency_key(_handler_arguments(request, call, runtime))
            or ""
        ).strip()
    except Exception:
        key = ""
    if not key:
        return call, completion.sources, "TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING"
    return replace(call, idempotency_key=key), completion.sources, ""


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
    return ToolResult.failed(
        call,
        _decision_message(decision),
        error_code=code,
        failure_stage=stage,
        facts=ToolFailureFacts(
            duration_ms=_elapsed_ms(started_at),
            status=status,
            metadata={
                "action_decision": decision.to_dict(),
                "input_sources": _source_dicts(input_sources),
            },
        ),
    )


def _invoke_with_operation_policy(
    request: ToolExecutorRequest,
    call: ToolCall,
    runtime: ToolRuntime,
    decision: ActionDecision,
) -> ToolHandlerOutcome:
    def invoke() -> ToolHandlerOutcome:
        if _cancelled(request):
            return ToolHandlerOutcome(
                call.tool_name,
                False,
                "tool cancelled before handler entry",
                error_code="CANCELLED",
                failure_stage=ToolFailureStage.RUNTIME_GATE.value,
                handler_executed=False,
            )
        return invoke_registry_tool(
            _invoke_request(
                request,
                call,
                operation_managed=decision.resolved_effect != "read_only",
            )
        )

    blocked = _require_operation_authority(request, call)
    if blocked is not None:
        return blocked
    if decision.resolved_effect == "read_only":
        outcome = invoke()
        if (
            not outcome.ok
            and outcome.retryable
            and outcome.handler_executed
            and not _cancelled(request)
        ):
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
            tool_name=call.tool_name,
            args_hash=call.args_hash,
            idempotency_key=call.idempotency_key,
            idempotency_scope=runtime.runtime_policy.idempotency_policy.scope,
            idempotency_namespace=call.tool_name,
            timeout_seconds=runtime.runtime_policy.timeout_policy.seconds,
            invoke=invoke,
            reconcile=_operation_reconciler(runtime, call),
            on_settled=_operation_settlement_notifier(runtime, call),
            resource_scopes=resource_scopes,
            attempt_id=call.attempt_id,
        )
    )


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
    require = getattr(request.operation_store, "require_authority", None)
    if require is None:
        return None
    try:
        require(
            ToolOperationAuthorityRequest(
                owner_id=request.operation_owner_id,
                run_id=call.run_id,
                task_id=_task_id(request),
                operation_id=call.operation_id,
                tool_name=call.tool_name,
                attempt_id=call.attempt_id,
            )
        )
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
        for root in extractor(
            call.arguments, request.write_boundary, request.workspace_root
        ):
            scopes = scopes + (f"workspace:{root}",)
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


# LLM: Handler invocation must receive the exact owner wall already authorized by ActionPolicy;
# it cannot fall back to mutable registry state. operation_managed is the host decision that tells
# recovery-aware handlers whether the generic operation store will send a later settlement notice.
# 函数用途: 把已通过权限门的调用和是否进入副作用账本的事实转成不可变 registry 执行请求。
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
    )


# LLM: Model arguments stay immutable; only declared internal parameters may receive host facts.
# The operation-managed flag must come from ActionPolicy, never model text or a handler guess.
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
    if "__cancellation_token" in internal_parameters:
        payload["__cancellation_token"] = request.cancellation_token
    return payload


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
    def reconcile(record: ToolOperationRecord) -> ToolOperationReconciliation:
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
                prior_result=dict(record.result or {}),
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
        "handler_details": dict(outcome.result_envelope or {}),
    }
    projection_error = str(projection.metadata.get("projection_error_code") or "")
    if projection_error:
        return ToolResult.failed(
            call,
            "The tool ran, but its output could not be archived safely.",
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
        effect_outcome=str(outcome.effect_outcome or _effect_from_operation_status(value)),
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


def _effect_from_operation_status(value: dict[str, Any]) -> str:
    status = str(value.get("status") or "").strip().lower()
    if status == "succeeded":
        return "confirmed"
    if status in {"not_started", "failed"}:
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


# LLM: 只渲染 ActionPolicy 的结构化裁决；公开 schema 和命令门位置可展示，原始参数值/密钥不可回显。
# 函数用途: 让模型和用户知道具体哪里被拦，减少盲猜参数；此处不改变授权结果、不自动重试。
def _decision_message(decision: ActionDecision) -> str:
    if decision.status == "ask":
        return "Approval or explicit user confirmation is required before this tool can run."
    message = "Tool execution was blocked before the handler ran: " + ", ".join(
        decision.reason_codes or ("RUNTIME_GATE_DENIED",)
    )
    boundary_error = str(decision.evidence.get("boundary_error") or "").strip()
    if boundary_error:
        message += f". {boundary_error}"
    rendered_issues = _render_validation_issues(decision.evidence.get("issues"))
    if rendered_issues:
        message += " 参数问题: " + rendered_issues
        allowed = decision.evidence.get("allowed_parameters")
        if isinstance(allowed, list) and all(isinstance(name, str) for name in allowed):
            message += "。该工具接受的参数: " + ", ".join(allowed)
    gate = decision.evidence.get("gate")
    if isinstance(gate, dict):
        message += _render_gate_findings(gate.get("findings"))
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
        path = str(item.get("path") or "").strip()
        keyword = str(item.get("keyword") or "").strip()
        expected = item.get("expected")
        actual = item.get("actual_type")
        if keyword == "required":
            parts.append(f"{path}: 必填缺失")
        elif keyword == "additionalProperties":
            parts.append(f"{path}: 未声明字段(该工具不接受额外参数)")
        elif keyword == "type":
            parts.append(f"{path}: 期望 {expected}, 实际 {actual or '?'}")
        else:
            parts.append(f"{path}: {keyword} 校验失败")
        if len(parts) >= 4:
            break
    return "; ".join(parts)


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
