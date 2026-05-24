# LLM: Offline recovery contracts turn structured failures into bounded retry and recovery decisions.
# 模块用途: 校验工具失败、状态损坏和 finalizer 崩溃后的恢复动作，避免模型靠自然语言声称可以继续。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .recovery_envelope import RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload

CORRUPT_STATE_ERROR_CODES = {"JSON_DECODE_ERROR", "STATE_SCHEMA_INVALID", "STATE_CHECKSUM_MISMATCH"}
NON_RETRYABLE_ERROR_CODES = {"PATH_PERMISSION_DENIED", "WRITE_FORBIDDEN", "APPROVAL_REJECTED"}


# LLM: OfflineRecoveryValidation reports blocking recovery findings and safe next actions.
# 类用途: 返回恢复合同是否通过、错误码、finding 和 action 建议。
@dataclass(frozen=True)
class OfflineRecoveryValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    actions: tuple[dict[str, str], ...]
    recovery: dict[str, Any] | None = None


# LLM: validate_recovery_events inspects structured runtime events for safe recovery decisions.
# 函数用途: 根据 tool_result、state_load_result 和 finalizer_crash 事件生成重试、阻断或重验收动作。
def validate_recovery_events(events: tuple[dict[str, Any], ...]) -> OfflineRecoveryValidation:
    findings: list[dict[str, str]] = []
    actions: list[dict[str, str]] = []
    for event in events:
        _apply_recovery_event(event, findings, actions)
    return OfflineRecoveryValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        actions=tuple(actions),
        recovery=_offline_recovery_payload(findings, actions),
    )


# LLM: _offline_recovery_payload gives offline validators the same repair envelope shape as runtime gates.
# 函数用途: 离线恢复合同失败时也输出可读中文和机器 next_status，避免调用方只看到 ok=false 后粗暴中断。
def _offline_recovery_payload(
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> dict[str, Any] | None:
    if not findings:
        return None
    next_statuses = {str(item.get("next_status") or "") for item in actions}
    status = "RECOVERING" if "VERIFYING" in next_statuses or "RECOVERING" in next_statuses else "BLOCKED"
    envelope = recovery_envelope_from_gate_payload(
        RecoveryEnvelopeRequest(
            gate="offline_recovery",
            status=status,
            allowed=False,
            findings=[
                {"code": item.get("code", ""), "severity": "P1", "message": "", "evidence": dict(item)}
                for item in findings
            ],
            recommended_action=str(actions[0].get("code") if actions else "stop_and_report_blocker"),
            evidence={"actions": [dict(item) for item in actions]},
        )
    )
    return envelope.to_dict() if envelope is not None else None


# LLM: _apply_recovery_event dispatches one structured event to the right recovery rule.
# 函数用途: 按 type 字段分派工具失败、状态读取和 finalizer 崩溃处理。
def _apply_recovery_event(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    event_type = _event_type(event)
    if event_type == "tool_result":
        _handle_tool_result(event, findings, actions)
    if event_type == "state_load_result":
        _handle_state_load_result(event, findings, actions)
    if event_type == "finalizer_crash":
        _handle_finalizer_crash(event, findings, actions)


# LLM: _handle_tool_result turns failed tool rows into retry or stop decisions.
# 函数用途: 可重试失败在预算内返回 RETRY_ALLOWED，超预算或不可重试失败返回阻断 finding。
def _handle_tool_result(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    if event.get("ok") is not False:
        return
    if not _retryable(event):
        findings.append(_finding("NON_RETRYABLE_FAILURE", event, "tool failure is not retryable"))
        actions.append(_action("STOP_RETRY", event, next_status="BLOCKED"))
        return
    retry_limit = _retry_limit(event)
    if retry_limit > 0 and _attempt(event) >= retry_limit:
        findings.append(_finding("RETRY_LIMIT_EXCEEDED", event, "retry limit reached"))
        actions.append(_action("STOP_RETRY", event, next_status="BLOCKED"))
        return
    actions.append(_action("RETRY_ALLOWED", event, next_status="RUNNING"))


# LLM: _handle_state_load_result reports corrupt state as a blocking recovery diagnostic.
# 函数用途: 状态读取失败且错误码属于损坏类时，阻断继续执行并建议进入 BLOCKED。
def _handle_state_load_result(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    if event.get("ok") is not False:
        return
    if _error_code(event) not in CORRUPT_STATE_ERROR_CODES:
        return
    findings.append(_finding("STATE_CORRUPT", event, _text(event.get("state_ref"))))
    actions.append(_action("STATE_CORRUPT", event, next_status="BLOCKED"))


# LLM: _handle_finalizer_crash prefers artifact revalidation over replaying completed side effects.
# 函数用途: finalizer 崩溃且已有产物时，要求先验收产物并禁止重放已执行副作用操作。
def _handle_finalizer_crash(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    artifact_refs = _string_list(event.get("artifact_refs"))
    if not artifact_refs:
        findings.append(_finding("FINALIZER_CRASH_WITHOUT_ARTIFACT", event, "artifact_refs missing"))
        actions.append(_action("RERUN_WITH_IDEMPOTENCY_CHECK", event, next_status="BLOCKED"))
        return
    findings.append(_finding("REVALIDATE_ARTIFACT_BEFORE_RERUN", event, artifact_refs[0]))
    actions.append(
        _action(
            "REVALIDATE_ARTIFACT_BEFORE_RERUN",
            event,
            next_status="VERIFYING",
            extra={
                "forbid_reexecute_operation_ids": ",".join(
                    _string_list(event.get("executed_side_effect_operation_ids"))
                )
            },
        )
    )


# LLM: _retryable derives retry permission from explicit fields and stable error codes.
# 函数用途: 优先读取 retryable 字段；缺失时用错误码集合判断是否不可重试。
def _retryable(event: dict[str, Any]) -> bool:
    if "retryable" in event:
        return bool(event.get("retryable"))
    return _error_code(event) not in NON_RETRYABLE_ERROR_CODES


# LLM: _attempt reads a one-based retry attempt counter from event fields.
# 函数用途: 把 attempt 字段规整为非负整数，无法解析时按 0 处理。
def _attempt(event: dict[str, Any]) -> int:
    return _int_field(event.get("attempt"))


# LLM: _retry_limit reads the retry budget from event fields with a conservative default.
# 函数用途: 把 retry_limit 字段规整为非负整数；显式 0 表示不启用重试次数上限。
def _retry_limit(event: dict[str, Any]) -> int:
    if "retry_limit" not in event:
        return 1
    return max(0, _int_field(event.get("retry_limit")))


# LLM: _action creates compact machine actions for recovery runners and tests.
# 函数用途: 生成 code、operation_id、next_status 以及可选额外字段。
def _action(
    code: str,
    event: dict[str, Any],
    *,
    next_status: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    return {
        "code": code,
        "operation_id": _text(event.get("operation_id")),
        "run_id": _text(event.get("run_id")),
        "next_status": next_status,
        **(extra or {}),
    }


# LLM: _finding creates compact machine findings for recovery validation.
# 函数用途: 生成 code、operation_id、run_id 和 detail 字段。
def _finding(code: str, event: dict[str, Any], detail: str) -> dict[str, str]:
    return {
        "code": code,
        "operation_id": _text(event.get("operation_id")),
        "run_id": _text(event.get("run_id")),
        "detail": detail,
    }


# LLM: _event_type normalizes event type values for exact recovery dispatch.
# 函数用途: 读取 type 字段并转为小写字符串。
def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


# LLM: _error_code normalizes structured error codes.
# 函数用途: 读取 error_code/code 字段并转大写。
def _error_code(event: dict[str, Any]) -> str:
    return _text(event.get("error_code") or event.get("code")).upper()


# LLM: _string_list normalizes list-like fields without parsing embedded prose.
# 函数用途: 把结构化数组规整成去空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in [_text(item)] if text]


# LLM: _int_field normalizes numeric event fields safely.
# 函数用途: 把整数或数字字符串转为 int，失败时返回 0。
def _int_field(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineRecoveryValidation", "validate_recovery_events"]
