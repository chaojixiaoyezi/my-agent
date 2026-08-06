from __future__ import annotations

from ...contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    evaluate_tool_guardrail_gate,
    record_tool_guardrail_result,
    result_hash_for_guardrail,
)
from ...tooling.models import tool_effect_for_runtime_policy
from ...tooling.runtime_contracts import ToolCall, ToolResult
from .call_guardrail_config import (
    readonly_no_progress_threshold,
    repeat_fail_threshold,
)
from .call_guardrail_config import (
    terminal_block_enabled as configured_terminal_block_enabled,
)

_RECORDS_ATTR = "_tool_call_guardrail_records"
_MAX_RECORDS = 256


def tool_guardrail_records(agent: object) -> tuple[dict[str, object], ...]:
    records = getattr(agent, _RECORDS_ATTR, None)
    if not isinstance(records, tuple):
        records = ()
    return tuple(dict(item) for item in records if isinstance(item, dict))


def tool_guardrail_policy(params: object) -> dict[str, object]:
    return {
        "repeat_fail_threshold": repeat_fail_threshold(params),
        "readonly_no_progress_threshold": readonly_no_progress_threshold(params),
        "terminal_block_enabled": configured_terminal_block_enabled(params),
    }


def record_tool_guard_observation(
    agent: object,
    runtime_params: object,
    call: ToolCall,
    result: ToolResult,
) -> str:
    facts = _facts_from_result(runtime_params, call, result)
    if result.ok and not facts.is_readonly:
        _set_tool_guardrail_records(
            agent,
            _records_without_readonly_no_progress(tool_guardrail_records(agent)),
        )
    records = record_tool_guardrail_result(
        tool_guardrail_records(agent),
        facts,
        max_records=_MAX_RECORDS,
    )
    _set_tool_guardrail_records(agent, records)
    decision = evaluate_tool_guardrail_gate(
        _facts_for_next_hint(facts),
        config=ToolGuardrailConfig(**tool_guardrail_policy(runtime_params)),
        records=records,
    )
    return decision.model_message if decision.allowed and decision.findings else ""


def _facts_from_result(
    runtime_params: object,
    call: ToolCall,
    result: ToolResult,
) -> ToolGuardrailFacts:
    effect = _resolved_effect(runtime_params, call, result)
    is_readonly = effect == "read_only"
    output_hash = result_hash_for_guardrail(result.output) if result.ok and is_readonly else ""
    return ToolGuardrailFacts(
        tool_name=call.tool_name,
        args_hash=call.args_hash,
        failed=not result.ok,
        result_hash=output_hash,
        is_readonly=is_readonly,
        failure_class=_failure_class(result) if not result.ok else "",
    )


def _resolved_effect(
    runtime_params: object,
    call: ToolCall,
    result: ToolResult,
) -> str:
    decision = result.metadata.get("action_decision")
    if isinstance(decision, dict):
        effect = str(decision.get("resolved_effect") or "").strip().lower()
        if effect in {"read_only", "mutating", "dangerous"}:
            return effect
    snapshot = getattr(runtime_params, "tool_runtime_snapshot", None)
    runtime = snapshot.runtime(call.tool_name) if snapshot is not None else None
    if runtime is None:
        return "dangerous"
    return tool_effect_for_runtime_policy(runtime.runtime_policy, call.arguments)


def _facts_for_next_hint(facts: ToolGuardrailFacts) -> ToolGuardrailFacts:
    return ToolGuardrailFacts(
        tool_name=facts.tool_name,
        args_hash=facts.args_hash,
        result_hash=facts.result_hash,
        is_readonly=facts.is_readonly,
        failure_class=facts.failure_class,
    )


def _records_without_readonly_no_progress(
    records: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        dict(record)
        for record in records
        if record.get("failed") is True or not str(record.get("result_hash") or "")
    )


def _set_tool_guardrail_records(
    agent: object,
    records: tuple[dict[str, object], ...],
) -> None:
    setattr(agent, _RECORDS_ATTR, records[-_MAX_RECORDS:])


def _failure_class(result: ToolResult) -> str:
    code = str(result.error_code or "").strip()
    if code:
        return f"code:{code}"
    category = str(result.error_category or "").strip()
    if category:
        return f"category:{category}"
    return f"output:{result_hash_for_guardrail(result.output)}"


__all__ = [
    "record_tool_guard_observation",
    "tool_guardrail_policy",
    "tool_guardrail_records",
]
