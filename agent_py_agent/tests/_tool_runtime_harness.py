from __future__ import annotations

"""Small test-only constructor for the canonical tool runtime path.

Tests pass an explicit tool name and explicit argument object.  The helper
creates a canonical ``ToolCall`` and sends it through ``ToolExecutor``; it does
not parse prose or recreate the deleted registry execution engine.
"""

from collections.abc import Callable
from pathlib import Path

from agent_py_agent.agent.tooling.cancellation import CancellationToken
from agent_py_agent.agent.tooling.executor import (
    ToolExecution,
    ToolExecutor,
    ToolExecutorRequest,
    ToolOutputProjection,
)
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolFailureFacts,
    ToolProtocolSnapshot,
    ToolResult,
    ToolSuccessFacts,
)


def make_test_model_spec(
    name: str,
    *,
    input_schema: dict[str, object] | None = None,
    description: str = "Canonical test tool.",
    category: str = "test",
    use_cases: tuple[str, ...] = (),
    avoid_when: tuple[str, ...] = (),
    keywords: tuple[str, ...] = (),
    examples: tuple[str, ...] = (),
) -> ToolModelSpec:
    """Build an exact canonical model contract for a test-only handler."""

    return ToolModelSpec(
        name=name,
        description=description,
        input_schema=input_schema
        or {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category=category,
            use_cases=use_cases,
            avoid_when=avoid_when,
            keywords=keywords,
            examples=examples,
        ),
    )


def make_test_runtime_policy(
    effect: str = "read_only",
    *,
    effect_by_parameter: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (),
    strategy: str = "declared",
    command_parameter: str = "",
    idempotency_scope: str | None = None,
    approval_mode: str = "dangerous",
    concurrency_mode: str | None = None,
    output_trust: str = "runtime",
    output_redaction: str = "default",
    resource_parameters: tuple[str, ...] = (),
) -> ToolRuntimePolicy:
    """Build an explicit canonical runtime policy for a test-only handler."""

    if idempotency_scope is None:
        idempotency_scope = "operation" if effect in {"mutating", "dangerous"} else ""
    if concurrency_mode is None:
        concurrency_mode = "parallel_safe" if effect == "read_only" else "serial"
    return ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            effect,
            by_parameter=effect_by_parameter,
            strategy=strategy,
            command_parameter=command_parameter,
        ),
        approval_policy=ApprovalPolicy(approval_mode),
        idempotency_policy=IdempotencyPolicy(idempotency_scope),
        concurrency_policy=ConcurrencyPolicy(concurrency_mode),
        resource_scopes=ResourceScopePolicy(
            parameter_names=resource_parameters,
        ),
        output_policy=OutputPolicy(
            trust=output_trust,
            redaction=output_redaction,
        ),
    )


def runtime_snapshot_for_tools(
    tools: dict[str, BaseTool],
    *,
    run_id: str = "test-run",
    allowed_tools: list[str] | None = None,
    owner_type: str = "main_agent",
) -> ToolRuntimeSnapshot:
    allowed = set(tools) if allowed_tools is None else set(allowed_tools)
    runtimes = tuple(
        _runtime_for_tool(tool)
        for name, tool in tools.items()
        if name in allowed
    )
    return ToolRuntimeSnapshot(
        run_id=run_id,
        runtimes=runtimes,
        available_tool_names=frozenset(runtime.model_spec.name for runtime in runtimes),
        unavailable_tools=(),
        allowed_tools=(frozenset(allowed_tools) if allowed_tools is not None else None),
        owner_type=owner_type,
    )


def runtime_snapshot_for_model_specs(
    specs: tuple[ToolModelSpec, ...],
    *,
    run_id: str = "test-run",
    policies: dict[str, ToolRuntimePolicy] | None = None,
    allowed_tools: list[str] | None = None,
    owner_type: str = "main_agent",
) -> ToolRuntimeSnapshot:
    """Build a canonical snapshot for protocol/IR tests that do not execute handlers."""

    policy_by_name = policies or {}
    runtimes = tuple(
        ToolRuntime(
            model_spec=spec,
            runtime_policy=policy_by_name.get(spec.name, make_test_runtime_policy()),
            handler=object(),
            availability=ToolAvailability.ready(),
        )
        for spec in specs
    )
    available = (
        frozenset(allowed_tools)
        if allowed_tools is not None
        else frozenset(spec.name for spec in specs)
    )
    return ToolRuntimeSnapshot(
        run_id=run_id,
        runtimes=runtimes,
        available_tool_names=available,
        unavailable_tools=(),
        allowed_tools=(frozenset(allowed_tools) if allowed_tools is not None else None),
        owner_type=owner_type,
    )


def make_test_protocol_snapshot(
    *,
    run_id: str = "test-run",
    source_protocol: str = "text",
) -> ToolProtocolSnapshot:
    """Declare the protocol selected at the start of one test run."""

    return ToolProtocolSnapshot(
        run_id=run_id,
        source_protocol=source_protocol,
        capability=ProviderToolCapability(
            provider="test",
            endpoint="local://test",
            model="fake",
            stream=False,
            native_supported=source_protocol == "native",
            evidence="explicit_test_protocol_fixture",
        ),
    )


def canonical_history_call(
    tool_name: str,
    arguments: dict[str, object],
    *,
    call_id: str = "history-call",
    source_protocol: str = "native",
    run_id: str = "history-run",
    turn_id: str = "history-turn",
    attempt_id: str = "history-attempt",
) -> ToolCall:
    """Create canonical history IR when no live runtime lookup is under test."""

    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        source_protocol=source_protocol,
        schema_hash="sha256:" + ("0" * 64),
        run_id=run_id,
        turn_id=turn_id,
        attempt_id=attempt_id,
    )


def canonical_history_result(
    call: ToolCall,
    content: str,
    *,
    ok: bool = True,
    error_code: str = "TOOL_EXECUTION_FAILED",
    handler_details: dict[str, object] | None = None,
    handler_executed: bool = True,
) -> ToolResult:
    """Create the sole ToolResult shape for chronology/archive unit tests."""

    metadata = {"handler_details": dict(handler_details or {})}
    if ok:
        return ToolResult.succeeded(
            call,
            content,
            facts=ToolSuccessFacts(
                metadata=metadata,
                handler_executed=handler_executed,
            ),
        )
    return ToolResult.failed(
        call,
        content,
        error_code=error_code,
        failure_stage="execution" if handler_executed else "validation",
        facts=ToolFailureFacts(
            handler_executed=handler_executed,
            metadata=metadata,
        ),
    )


def _runtime_for_tool(tool: BaseTool) -> ToolRuntime:
    model_spec = getattr(tool, "model_spec", None)
    runtime_policy = getattr(tool, "runtime_policy", None)
    if not isinstance(model_spec, ToolModelSpec) or not isinstance(
        runtime_policy, ToolRuntimePolicy
    ):
        raise TypeError("test tool must declare ToolModelSpec and ToolRuntimePolicy")
    availability = (
        tool.availability() if hasattr(tool, "availability") else ToolAvailability.ready()
    )
    return ToolRuntime(
        model_spec=model_spec,
        runtime_policy=runtime_policy,
        handler=tool,
        availability=availability,
    )


def canonical_test_call(
    snapshot: ToolRuntimeSnapshot,
    tool_name: str,
    arguments: dict[str, object],
    *,
    call_id: str = "test-call",
    attempt_id: str = "test-attempt",
    operation_id: str = "",
    idempotency_key: str = "",
) -> ToolCall:
    runtime = snapshot.runtime(tool_name)
    schema_hash = runtime.model_spec.schema_hash if runtime is not None else "sha256:" + ("0" * 64)
    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        source_protocol="native",
        schema_hash=schema_hash,
        run_id=snapshot.run_id,
        turn_id="test-turn",
        attempt_id=attempt_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )


def execute_canonical_test_call(
    root: Path,
    *,
    tools: dict[str, BaseTool],
    tool_name: str,
    arguments: dict[str, object],
    run_id: str = "test-run",
    allowed_tools: list[str] | None = None,
    write_boundary: dict[str, object] | None = None,
    workspace_roots: tuple[Path, ...] | None = None,
    path_dangerous_roots: tuple[str, ...] = (),
    owner_scope_root: str = "",
    runtime_guard_policy: object | None = None,
    trusted_run_context: dict[str, object] | None = None,
    operation_store: object | None = None,
    operation_store_required: bool = False,
    operation_owner_id: str = "test-owner",
    cancellation_token: CancellationToken | None = None,
    pre_handler_gate: Callable[[ToolCall], ToolHandlerOutcome | None] | None = None,
    output_archiver: Callable[[ToolCall, object], ToolOutputProjection] | None = None,
    call_id: str = "test-call",
    attempt_id: str = "test-attempt",
    operation_id: str = "",
    idempotency_key: str = "",
) -> ToolExecution:
    snapshot = runtime_snapshot_for_tools(
        tools,
        run_id=run_id,
        allowed_tools=allowed_tools,
    )
    call = canonical_test_call(
        snapshot,
        tool_name,
        arguments,
        call_id=call_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )
    return ToolExecutor().execute(
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=root,
            workspace_roots=workspace_roots or (root,),
            path_dangerous_roots=path_dangerous_roots,
            owner_scope_root=owner_scope_root,
            write_boundary=write_boundary,
            runtime_guard_policy=runtime_guard_policy,
            trusted_run_context=trusted_run_context,
            operation_store=operation_store,
            operation_store_required=operation_store_required,
            operation_owner_id=operation_owner_id,
            cancellation_token=cancellation_token,
            pre_handler_gate=pre_handler_gate,
            output_archiver=output_archiver,
        )
    )


def execute_registry_test_call(
    registry: object,
    tool_name: str,
    arguments: dict[str, object],
    *,
    allowed_tools: list[str] | None = None,
    write_boundary: dict[str, object] | None = None,
    trusted_run_context: dict[str, object] | None = None,
    cancellation_token: CancellationToken | None = None,
    required_action: object | None = None,
    pre_handler_gate: Callable[[ToolCall], ToolHandlerOutcome | None] | None = None,
    output_archiver: Callable[[ToolCall, object], ToolOutputProjection] | None = None,
    run_id: str = "test-run",
    call_id: str = "test-call",
    attempt_id: str = "test-attempt",
    operation_id: str = "",
    idempotency_key: str = "",
) -> ToolResult:
    """Exercise a real registry through its canonical ToolCall-only entry."""

    snapshot = registry.runtime_snapshot(allowed_tools=allowed_tools, run_id=run_id)
    call = canonical_test_call(
        snapshot,
        tool_name,
        arguments,
        call_id=call_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )
    return registry.execute_tool(
        call,
        write_boundary=write_boundary,
        runtime_snapshot=snapshot,
        trusted_run_context=trusted_run_context,
        cancellation_token=cancellation_token,
        required_action=required_action,
        pre_handler_gate=pre_handler_gate,
        output_archiver=output_archiver,
    ).result


def execute_approved_registry_test_call(
    registry: object,
    tool_name: str,
    arguments: dict[str, object],
    *,
    allowed_tools: list[str] | None = None,
    write_boundary: dict[str, object] | None = None,
    run_id: str = "test-run",
    call_id: str = "test-call",
    attempt_id: str = "test-attempt",
) -> ToolResult:
    """Execute one dangerous registry call with its exact generated approval binding."""

    snapshot = registry.runtime_snapshot(allowed_tools=allowed_tools, run_id=run_id)
    call = canonical_test_call(
        snapshot,
        tool_name,
        arguments,
        call_id=call_id,
        attempt_id=attempt_id,
    )
    first = registry.execute_tool(
        call,
        write_boundary=write_boundary,
        runtime_snapshot=snapshot,
    )
    if first.result.status != "approval_required" or not first.decision.approval_request:
        raise AssertionError(
            f"expected approval_required before approved test execution: {tool_name}"
        )
    binding: dict[str, object] = {
        **dict(first.decision.approval_request),
        "approval_id": f"approval-{call_id}",
        "status": "APPROVED",
    }
    approved_boundary = dict(write_boundary or {})
    approved_boundary["approved_actions"] = [
        *(approved_boundary.get("approved_actions") or []),
        binding,
    ]
    return registry.execute_tool(
        call,
        runtime_snapshot=snapshot,
        write_boundary=approved_boundary,
    ).result


__all__ = [
    "canonical_test_call",
    "execute_canonical_test_call",
    "execute_approved_registry_test_call",
    "execute_registry_test_call",
    "runtime_snapshot_for_tools",
    "make_test_model_spec",
    "make_test_runtime_policy",
]
