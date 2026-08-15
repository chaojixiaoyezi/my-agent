from __future__ import annotations

from ...contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    evaluate_tool_guardrail_gate,
    failure_class_of_result,
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


def clear_consecutive_failure_segment(
    agent: object,
    tool_name: str,
    failure_class: str,
) -> None:
    """清掉某工具某失败类的尾部连续段,使计数从 0 重新累计。

    收口后调用:模型换策略重新尝试同一工具时,不应因为历史段未清而
    一碰就再次收口。边界规则与 consecutive_same_failure_count 一致
    (同工具成功/不同失败类/guardrail 自身记录都不在段内)。
    """
    records = list(tool_guardrail_records(agent))
    if not records:
        return
    drop_indices: set[int] = set()
    for i in range(len(records) - 1, -1, -1):
        record = records[i]
        if str(record.get("tool_name") or "") != tool_name:
            continue
        current_class = str(record.get("failure_class") or "")
        if current_class.startswith("code:TOOL_GUARDRAIL"):
            continue
        if record.get("failed") is not True or current_class != failure_class:
            break
        drop_indices.add(i)
    if not drop_indices:
        return
    remaining = [r for i, r in enumerate(records) if i not in drop_indices]
    _set_tool_guardrail_records(agent, tuple(remaining))


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
    return failure_class_of_result(result)


__all__ = [
    "record_tool_guard_observation",
    "tool_guardrail_policy",
    "tool_guardrail_records",
]
