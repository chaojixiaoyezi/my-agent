# LLM: Offline event-order contracts validate async event streams before state changes are trusted.
# 模块用途: 校验重复、乱序、迟到、过期和错误 run_id 事件。

from __future__ import annotations

from typing import Any

from .offline_contract_report import OfflineContractValidation, finding, text, validation_report

TERMINAL_EVENTS = {"TASK_CANCELLED", "TASK_BLOCKED", "TASK_FAILED", "TASK_SUCCEEDED"}
TOOL_RESULT_EVENTS = {"TOOL_OK", "TOOL_FAILED"}


# LLM: validate_event_order checks event identity and ordering from structured event rows.
# 函数用途: 用 event_id、run_id、type 校验事件流，不读取自然语言消息。
def validate_event_order(
    *,
    run_id: str,
    events: tuple[dict[str, Any], ...],
) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    seen: set[str] = set()
    ignored: list[str] = []
    waiting_tool = False
    terminal = False
    for event in events:
        event_id = text(event.get("event_id"))
        if event_id and event_id in seen:
            ignored.append(event_id)
            continue
        if event_id:
            seen.add(event_id)
        event_type = text(event.get("type")).upper()
        if _run_scope_mismatch(run_id, event, findings):
            continue
        if _validate_late_event(terminal, event_type, findings):
            continue
        _validate_tool_order(waiting_tool, event_type, findings)
        waiting_tool = _next_waiting_tool(waiting_tool, event_type)
        terminal = terminal or event_type in TERMINAL_EVENTS
    return validation_report(findings, ignored_event_ids=tuple(ignored))


# LLM: _validate_run_scope rejects events for old or different runs.
# 函数用途: run_id 不匹配时生成 EVENT_RUN_ID_MISMATCH。
def _run_scope_mismatch(run_id: str, event: dict[str, Any], findings: list[dict[str, object]]) -> bool:
    if text(event.get("run_id")) != run_id:
        findings.append(finding("EVENT_RUN_ID_MISMATCH", {"event_id": text(event.get("event_id"))}))
        return True
    return False


# LLM: _validate_late_event rejects tool/approval events after terminal states.
# 函数用途: 终态后 TOOL_* 返回 EVENT_AFTER_TERMINAL，APPROVED 返回 APPROVAL_AFTER_BLOCKED。
def _validate_late_event(terminal: bool, event_type: str, findings: list[dict[str, object]]) -> bool:
    if not terminal:
        return False
    if event_type in TOOL_RESULT_EVENTS:
        findings.append(finding("EVENT_AFTER_TERMINAL"))
        return True
    if event_type == "APPROVED":
        findings.append(finding("APPROVAL_AFTER_BLOCKED"))
        return True
    return False


# LLM: _validate_tool_order requires tool results to follow NEED_TOOL.
# 函数用途: 未进入 WAITING_TOOL 就收到 TOOL_OK/TOOL_FAILED 时返回 EVENT_OUT_OF_ORDER。
def _validate_tool_order(waiting_tool: bool, event_type: str, findings: list[dict[str, object]]) -> None:
    if event_type in TOOL_RESULT_EVENTS and not waiting_tool:
        findings.append(finding("EVENT_OUT_OF_ORDER"))


# LLM: _next_waiting_tool updates the small local waiting flag.
# 函数用途: NEED_TOOL 打开等待，TOOL_OK/TOOL_FAILED 关闭等待。
def _next_waiting_tool(waiting_tool: bool, event_type: str) -> bool:
    if event_type == "NEED_TOOL":
        return True
    if event_type in TOOL_RESULT_EVENTS:
        return False
    return waiting_tool


__all__ = ["validate_event_order"]
