
from __future__ import annotations

import hashlib
import json
from typing import Any

from ...contracts.gates.tool_effects import args_hash_for_call
from ...contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    evaluate_tool_guardrail_gate,
    record_tool_guardrail_result,
    result_hash_for_guardrail,
)
from ...contracts.tool_protocol_v2 import (
    execution_payload_for_tool_protocol,
    normalize_tool_call,
)
from ...tooling.models import (
    ToolExecutionResult,
    ToolSpec,
    tool_effect_for_parameters,
)
from ...tooling.tool_spec_schema import tool_spec_declared_input_fields
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


def record_tool_guard_observation(agent: object, runtime_params: object, payload: object, result: ToolExecutionResult) -> str:
    if not isinstance(payload, dict):
        return ""
    facts = _facts_from_result(agent, payload, result)
    if not facts.tool_name:
        return ""
    if result.ok and not facts.is_readonly:
        _set_tool_guardrail_records(agent, _records_without_readonly_no_progress(tool_guardrail_records(agent)))
    records = record_tool_guardrail_result(tool_guardrail_records(agent), facts, max_records=_MAX_RECORDS)
    _set_tool_guardrail_records(agent, records)
    decision = evaluate_tool_guardrail_gate(
        _facts_for_next_hint(facts),
        config=ToolGuardrailConfig(**tool_guardrail_policy(runtime_params)),
        records=records,
    )
    return decision.model_message if decision.allowed and decision.findings else ""


def _facts_from_result(
    agent: object,
    payload: dict[str, object],
    result: ToolExecutionResult,
) -> ToolGuardrailFacts:
    tool_name, args_hash, input_payload, spec = _tool_identity(agent, payload)
    is_readonly = (
        tool_effect_for_parameters(spec, input_payload) == "read_only"
        if spec is not None
        else False
    )
    output_hash = ""
    if result.ok and is_readonly:
        output_hash = result_hash_for_guardrail(result.output)
    return ToolGuardrailFacts(
        tool_name=tool_name or str(result.tool or "").strip(),
        args_hash=args_hash,
        failed=not result.ok,
        result_hash=output_hash,
        is_readonly=is_readonly,
        failure_class=_failure_class(result) if not result.ok else "",
    )


def _facts_for_next_hint(facts: ToolGuardrailFacts) -> ToolGuardrailFacts:
    return ToolGuardrailFacts(
        tool_name=facts.tool_name,
        args_hash=facts.args_hash,
        result_hash=facts.result_hash,
        is_readonly=facts.is_readonly,
        failure_class=facts.failure_class,
    )


def _tool_identity(
    agent: object,
    payload: dict[str, object],
) -> tuple[str, str, dict[str, Any], ToolSpec | None]:
    name = str(payload.get("tool") or payload.get("tool_name") or "").strip()
    spec = _registered_tool_spec(agent, name)
    declared = tool_spec_declared_input_fields(spec) if spec is not None else ()
    canonical = execution_payload_for_tool_protocol(
        payload,
        declared_input_fields=declared,
    )
    normalized = normalize_tool_call(canonical)
    return normalized.tool_name, args_hash_for_call(normalized.input), normalized.input, spec


def _registered_tool_spec(agent: object, tool_name: str) -> ToolSpec | None:
    registry = getattr(agent, "tools", None)
    tools = getattr(registry, "tools", registry if isinstance(registry, dict) else None)
    tool = tools.get(tool_name) if isinstance(tools, dict) else None
    spec = getattr(tool, "spec", None)
    return spec if isinstance(spec, ToolSpec) else None


def _records_without_readonly_no_progress(records: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    kept: list[dict[str, object]] = []
    for record in records:
        if record.get("failed") is not True and str(record.get("result_hash") or ""):
            continue
        kept.append(dict(record))
    return tuple(kept)


def _set_tool_guardrail_records(agent: object, records: tuple[dict[str, object], ...]) -> None:
    setattr(agent, _RECORDS_ATTR, records[-_MAX_RECORDS:])


def _failure_class(result: ToolExecutionResult) -> str:
    code = str(getattr(result, "error_code", "") or "").strip()
    if code:
        return f"code:{code}"
    category = str(getattr(result, "error_category", "") or "").strip()
    if category:
        return f"category:{category}"
    return f"output:{result_hash_for_guardrail(result.output)}"


__all__ = [
    "record_tool_guard_observation",
    "tool_guardrail_policy",
    "tool_guardrail_records",
]
