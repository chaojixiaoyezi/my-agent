from __future__ import annotations


def append_tool_guardrail_action_block_hint(request: object) -> None:
    """Append a model-facing hint when the registry blocks the repeated tool action."""
    current_round_prefix = f"{getattr(request, 'tool_rounds', '')}-"
    params = getattr(request, "params", None)
    archive_records = getattr(params, "archive_tool_calls", []) or []
    for record in reversed(list(archive_records)):
        if not isinstance(record, dict):
            continue
        if not str(record.get("call_id") or "").startswith(current_round_prefix):
            continue
        gate = record.get("runtime_gate")
        if not _is_tool_guardrail_action_block(gate):
            continue
        model_message = str(gate.get("model_message") or "").strip()
        reason = _first_gate_finding_code(gate) or str(gate.get("operator_message") or "TOOL_GUARDRAIL_ACTION_BLOCKED")
        tool_context = getattr(params, "tool_context", None)
        if isinstance(tool_context, list):
            tool_context.append(
                "[tool-loop-guardrail-hint]\n"
                f"{reason}：同一工具路径持续无效，这一次相同工具调用未执行。"
                f"{model_message or '请换关键词、换参数、换工具或换数据来源，再继续推进任务。'}"
            )
        return


def _is_tool_guardrail_action_block(gate: object) -> bool:
    if not isinstance(gate, dict):
        return False
    if gate.get("gate") != "tool_guardrail" or gate.get("allow_action") is not False:
        return False
    return any(
        code in {"TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED", "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED"}
        for code in _gate_finding_codes(gate)
    )


def _gate_finding_codes(gate: dict[str, object]) -> tuple[str, ...]:
    findings = gate.get("findings")
    if not isinstance(findings, list):
        return ()
    codes: list[str] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            codes.append(code)
    return tuple(codes)


def _first_gate_finding_code(gate: dict[str, object]) -> str:
    for code in _gate_finding_codes(gate):
        return code
    return ""


__all__ = ["append_tool_guardrail_action_block_hint"]
