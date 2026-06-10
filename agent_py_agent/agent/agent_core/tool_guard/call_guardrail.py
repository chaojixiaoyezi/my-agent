
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
from ...contracts.tool_protocol_v2 import normalize_tool_call
from ...tooling.models import ToolExecutionResult
from .call_guardrail_config import (
    readonly_no_progress_threshold,
    repeat_fail_threshold,
)
from .call_guardrail_config import (
    terminal_block_enabled as configured_terminal_block_enabled,
)

_RECORDS_ATTR = "_tool_call_guardrail_records"
_MAX_RECORDS = 256
_READ_ONLY_TOOL_NAMES = {
    "list_files",
    "list_tools",
    "read_artifact",
    "read_file",
    "search",
    "search_text",
    "web_search",
    "web_fetch",
}
_LOCAL_PROGRESS_TOOL_NAMES = {
    "apply_patch",
    "run_command",
    "write_file",
}

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
    facts = _facts_from_result(payload, result)
    if not facts.tool_name:
        return ""
    if result.ok and _is_local_progress_tool(facts.tool_name):
        _set_tool_guardrail_records(agent, _records_without_readonly_no_progress(tool_guardrail_records(agent)))
    records = record_tool_guardrail_result(tool_guardrail_records(agent), facts, max_records=_MAX_RECORDS)
    _set_tool_guardrail_records(agent, records)
    decision = evaluate_tool_guardrail_gate(
        _facts_for_next_hint(facts),
        config=ToolGuardrailConfig(**tool_guardrail_policy(runtime_params)),
        records=records,
    )
    return decision.model_message if decision.allowed and decision.findings else ""


def _facts_from_result(payload: dict[str, object], result: ToolExecutionResult) -> ToolGuardrailFacts:
    tool_name, args_hash = _tool_identity(payload)
    output_hash = ""
    if result.ok and _is_read_only_tool(tool_name):
        output_hash = result_hash_for_guardrail(result.output)
    return ToolGuardrailFacts(
        tool_name=tool_name or str(result.tool or "").strip(),
        args_hash=args_hash,
        failed=not result.ok,
        result_hash=output_hash,
        is_readonly=_is_read_only_tool(tool_name),
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


def _tool_identity(payload: dict[str, object]) -> tuple[str, str]:
    normalized = normalize_tool_call(_payload_for_guardrail(payload))
    return normalized.tool_name, args_hash_for_call(normalized.input)


def _payload_for_guardrail(payload: dict[str, object]) -> dict[str, object]:
    if "tool_name" in payload and "input" in payload:
        return payload
    if "tool" not in payload:
        return payload
    input_payload = {key: value for key, value in payload.items() if key not in {"tool", "kind"}}
    return {"tool_name": str(payload.get("tool") or ""), "input": input_payload}


def _records_without_readonly_no_progress(records: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    kept: list[dict[str, object]] = []
    for record in records:
        if record.get("failed") is not True and str(record.get("result_hash") or ""):
            continue
        kept.append(dict(record))
    return tuple(kept)


def _set_tool_guardrail_records(agent: object, records: tuple[dict[str, object], ...]) -> None:
    setattr(agent, _RECORDS_ATTR, records[-_MAX_RECORDS:])


def _is_read_only_tool(tool_name: str) -> bool:
    return tool_name in _READ_ONLY_TOOL_NAMES


def _is_local_progress_tool(tool_name: str) -> bool:
    return tool_name in _LOCAL_PROGRESS_TOOL_NAMES


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
