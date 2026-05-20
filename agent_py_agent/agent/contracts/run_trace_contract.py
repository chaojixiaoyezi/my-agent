# LLM: Run trace contracts validate replayable state and tool ledgers from structured events.
# 模块用途: 校验运行事件是否包含可恢复所需的状态迁移、工具调用结果、错误码和耗时字段。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state_machine_transitions import transition_contract


# LLM: RunTraceValidation is the machine-readable report for ledger/replay checks.
# 类用途: 返回 run trace 是否通过、错误码和具体事件位置 findings。
@dataclass(frozen=True)
class RunTraceValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]


# LLM: validate_run_trace_events checks state/tool events without using final prose summaries.
# 函数用途: 校验状态迁移是否合法、RunLog 字段是否完整、ToolTrace 是否可回放。
def validate_run_trace_events(events: tuple[dict[str, Any], ...]) -> RunTraceValidation:
    findings: list[dict[str, str]] = []
    for index, event in enumerate(events):
        event_type = str(event.get("type") or "")
        if event_type == "state_transition":
            _validate_state_transition(index, event, findings)
        if event_type == "tool_result":
            _validate_tool_result(index, event, findings)
    return RunTraceValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_state_transition enforces replayable lifecycle facts for one ledger row.
# 函数用途: 检查状态迁移事件的 run_id/from/event/to 字段和共享状态机合法性。
def _validate_state_transition(index: int, event: dict[str, Any], findings: list[dict[str, str]]) -> None:
    _append_required_field_findings(
        findings,
        (
            _required_field_finding(index, event, "run_id", "RUNLOG_RUN_ID_MISSING"),
            _required_field_finding(index, event, "from", "RUNLOG_FROM_STATUS_MISSING"),
            _required_field_finding(index, event, "event", "RUNLOG_EVENT_MISSING"),
            _required_field_finding(index, event, "to", "RUNLOG_TO_STATUS_MISSING"),
        ),
    )
    contract = transition_contract(event.get("from"), event.get("to"))
    if not contract.allowed:
        findings.append(
            _finding(
                index,
                "STATE_TRANSITION_INVALID",
                f"{contract.from_status}->{contract.to_status}:{contract.reason}",
            )
        )


# LLM: _validate_tool_result enforces the minimum fields needed to recover and replay tool work.
# 函数用途: 检查工具结果事件的 run_id/tool/operation_id/duration_ms，失败时必须有 error_code。
def _validate_tool_result(index: int, event: dict[str, Any], findings: list[dict[str, str]]) -> None:
    _append_required_field_findings(
        findings,
        (
            _required_field_finding(index, event, "run_id", "TOOL_TRACE_RUN_ID_MISSING"),
            _required_field_finding(index, event, "tool", "TOOL_TRACE_TOOL_MISSING"),
            _required_field_finding(index, event, "operation_id", "TOOL_TRACE_OPERATION_ID_MISSING"),
        ),
    )
    if not _has_duration(event):
        findings.append(_finding(index, "TOOL_TRACE_DURATION_MISSING", "duration_ms is required"))
    if event.get("ok") is False and not str(event.get("error_code") or ""):
        findings.append(_finding(index, "TOOL_TRACE_ERROR_CODE_MISSING", "failed tool_result needs error_code"))


# LLM: _required_field_finding reports one absent structured field as an optional finding.
# 函数用途: 对单个事件字段做非空校验；存在则返回 None，缺失则返回 finding。
def _required_field_finding(index: int, event: dict[str, Any], field: str, code: str) -> dict[str, str] | None:
    if str(event.get(field) or ""):
        return None
    return _finding(index, code, f"{field} is required")


# LLM: _append_required_field_findings keeps validators small without widening helper signatures.
# 函数用途: 追加非空 finding，过滤字段存在时返回的 None。
def _append_required_field_findings(
    findings: list[dict[str, str]],
    candidates: tuple[dict[str, str] | None, ...],
) -> None:
    findings.extend(item for item in candidates if item is not None)


# LLM: _has_duration accepts integer and float durations while rejecting missing or negative values.
# 函数用途: 判断工具结果是否记录了非负 duration_ms。
def _has_duration(event: dict[str, Any]) -> bool:
    value = event.get("duration_ms")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return value >= 0


# LLM: _finding keeps trace validation diagnostics stable and compact.
# 函数用途: 生成带 index/code/detail 的 finding，方便测试和报告按机器字段消费。
def _finding(index: int, code: str, detail: str) -> dict[str, str]:
    return {"index": str(index), "code": code, "detail": detail}


__all__ = ["RunTraceValidation", "validate_run_trace_events"]
