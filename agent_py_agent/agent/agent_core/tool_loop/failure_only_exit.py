from __future__ import annotations

"""Deterministic exit guard for runs whose every real tool attempt was blocked."""

import json

from ...backends import ModelResponse
from ...contracts.error_taxonomy import error_contract
from ...contracts.recovery import RecoveryAction

_MARKER = "[RUN_TOOL_EVIDENCE_BLOCKED]"


def blocked_tool_only_exit_response(params: object, final_response: object) -> ModelResponse | None:
    """Replace unverifiable prose when all current-run attempts ended in terminal blockers."""

    records = _current_run_tool_records(params)
    if not records or any(record.get("ok") is True for record in records):
        return None
    failures = [record for record in records if record.get("ok") is False]
    blocker_codes = _terminal_blocker_codes(failures)
    if not failures or not blocker_codes:
        return None
    tools = tuple(dict.fromkeys(str(record.get("tool") or "unknown") for record in failures))
    payload = {
        "status": "unfinished",
        "reason": "all_tool_attempts_blocked",
        "successful_tool_calls": 0,
        "failed_tool_calls": len(failures),
        "error_codes": list(blocker_codes),
        "attempted_tools": list(tools),
    }
    text = (
        "本轮没有获得任何可验证的工具结果，不能把推测值或模型常识写成实际执行结果。\n"
        f"阻塞错误：{', '.join(blocker_codes)}。修复执行环境或权限后再重试。\n\n"
        f"{_MARKER}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )
    return ModelResponse(
        text=text,
        backend=str(getattr(final_response, "backend", "") or ""),
        runtime_status="unfinished",
        runtime_reason="ALL_TOOL_ATTEMPTS_BLOCKED",
        runtime_source="tool_evidence_guard",
    )


def _current_run_tool_records(params: object) -> list[dict[str, object]]:
    records = [
        record
        for record in list(getattr(params, "archive_tool_calls", None) or [])
        if isinstance(record, dict) and str(record.get("tool") or "") and "ok" in record
    ]
    request_id = str(getattr(params, "request_id", "") or "")
    if not request_id:
        return records
    return [record for record in records if str(record.get("request_id") or "") == request_id]


def _terminal_blocker_codes(records: list[dict[str, object]]) -> tuple[str, ...]:
    codes: list[str] = []
    for record in records:
        code = str(record.get("error_code") or "").strip().upper()
        if not code:
            continue
        contract = error_contract(code)
        if contract.retryable or contract.recommended_action != RecoveryAction.REPORT_BLOCKER.value:
            continue
        codes.append(code)
    return tuple(dict.fromkeys(codes))


__all__ = ["blocked_tool_only_exit_response"]
