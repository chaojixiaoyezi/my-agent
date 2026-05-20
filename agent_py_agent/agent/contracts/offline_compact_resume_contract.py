# LLM: Offline compact/resume contracts ensure compressed state remains replay-safe.
# 模块用途: 校验上下文压缩包是否保留等待状态、工具引用、失败操作和副作用重放边界。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

WAITING_STATUSES = {"WAITING_FOR_TOOL", "WAITING_FOR_USER", "WAITING_FOR_CHILD", "WAITING_HUMAN"}
SUMMARY_REQUIRED_FIELDS = (
    "current_status",
    "completed_actions",
    "pending_actions",
    "evidence_refs",
    "next_constraints",
)


# LLM: OfflineCompactResumeValidation reports contract findings for compact/replay tests.
# 类用途: 返回 compact/resume 合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineCompactResumeValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: validate_compact_resume_bundle checks one structured compact bundle and resume event list.
# 函数用途: 读取 pre_compact、pre_compact_events、compact、resume_events 字段并做离线合同校验。
def validate_compact_resume_bundle(bundle: dict[str, Any]) -> OfflineCompactResumeValidation:
    findings: list[dict[str, object]] = []
    pre_compact = _section(bundle.get("pre_compact"))
    compact = _section(bundle.get("compact"))
    _validate_waiting_state(pre_compact, compact, findings)
    _validate_tool_result_refs(pre_compact, compact, findings)
    _validate_failed_operations(bundle, compact, findings)
    _validate_side_effect_replay(compact, _event_list(bundle.get("resume_events")), findings)
    _validate_summary(compact, findings)
    return OfflineCompactResumeValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_waiting_state blocks compact bundles that forget an active waiting status.
# 函数用途: 如果压缩前任务在等待工具、人或子任务，compact 必须保留同一个 current_status。
def _validate_waiting_state(
    pre_compact: dict[str, Any],
    compact: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    previous_status = _status(pre_compact.get("current_status"))
    if previous_status not in WAITING_STATUSES:
        return
    compact_status = _status(compact.get("current_status"))
    if compact_status != previous_status:
        findings.append(
            _finding(
                "COMPACT_STATE_MISSING",
                {
                    "expected_status": previous_status,
                    "compact_status": compact_status,
                    "waiting_reason": _text(pre_compact.get("waiting_reason")),
                },
            )
        )


# LLM: _validate_tool_result_refs ensures archived tool outputs remain addressable after compact.
# 函数用途: 压缩前已有 tool_result_refs 时，compact 必须保留同一批引用，不能靠摘要正文替代。
def _validate_tool_result_refs(
    pre_compact: dict[str, Any],
    compact: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    previous_refs = set(_string_list(pre_compact.get("tool_result_refs")))
    if not previous_refs:
        return
    compact_refs = set(_string_list(compact.get("tool_result_refs")))
    missing_refs = sorted(previous_refs - compact_refs)
    if missing_refs:
        findings.append(_finding("COMPACT_TOOL_REF_MISSING", {"missing_refs": missing_refs}))


# LLM: _validate_failed_operations carries failed operation ids into the compact bundle.
# 函数用途: 从 pre_compact 和 pre_compact_events 读取失败 operation_id，要求 compact.failed_operation_ids 保留。
def _validate_failed_operations(
    bundle: dict[str, Any],
    compact: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    failed_ids = set(_string_list(_section(bundle.get("pre_compact")).get("failed_operation_ids")))
    for event in _event_list(bundle.get("pre_compact_events")):
        if operation_id := _failed_event_operation_id(event):
            failed_ids.add(operation_id)
    if not failed_ids:
        return
    compact_failed_ids = set(_string_list(compact.get("failed_operation_ids")))
    missing_ids = sorted(failed_ids - compact_failed_ids)
    if missing_ids:
        findings.append(_finding("COMPACT_FAILURE_FORGOTTEN", {"missing_operation_ids": missing_ids}))


# LLM: _failed_event_operation_id extracts failed tool operation ids from structured events.
# 函数用途: 只在 tool_result.ok=false 且 operation_id 非空时返回 id。
def _failed_event_operation_id(event: dict[str, Any]) -> str:
    if _event_type(event) != "tool_result" or event.get("ok") is not False:
        return ""
    return _text(event.get("operation_id"))


# LLM: _validate_side_effect_replay prevents resume from reissuing completed side-effect operations.
# 函数用途: compact 记录已执行副作用 operation_id 后，resume_events 中同 id 的 tool_call 视为重放。
def _validate_side_effect_replay(
    compact: dict[str, Any],
    resume_events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    executed_ids = set(_string_list(compact.get("executed_side_effect_operation_ids")))
    if not executed_ids:
        return
    replayed = sorted(
        _text(event.get("operation_id"))
        for event in resume_events
        if _event_type(event) == "tool_call" and _text(event.get("operation_id")) in executed_ids
    )
    if replayed:
        findings.append(_finding("DANGEROUS_OPERATION_REPLAYED", {"operation_ids": replayed}))


# LLM: _validate_summary checks the compact prompt section has enough machine-addressable anchors.
# 函数用途: summary 必须有状态、完成动作、待办动作、证据引用和下一步限制这些结构字段。
def _validate_summary(compact: dict[str, Any], findings: list[dict[str, object]]) -> None:
    summary = _section(compact.get("summary"))
    missing_fields = [
        field
        for field in SUMMARY_REQUIRED_FIELDS
        if _field_missing(summary, field)
    ]
    if missing_fields:
        findings.append(_finding("COMPACT_SUMMARY_INCOMPLETE", {"missing_fields": missing_fields}))


# LLM: _field_missing treats scalar and list fields as present only when non-empty.
# 函数用途: 对 summary 字段做空值判断，不解析字段内容含义。
def _field_missing(summary: dict[str, Any], field: str) -> bool:
    value = summary.get(field)
    if isinstance(value, (list, tuple, set)):
        return not _string_list(value)
    return not _text(value)


# LLM: _finding creates compact machine findings without prose parsing.
# 函数用途: 生成 code 和额外结构字段。
def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: _event_list normalizes event arrays from test fixtures and replay files.
# 函数用途: 只接受 dict 事件列表，忽略无结构项。
def _event_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# LLM: _section normalizes nested dict sections.
# 函数用途: 非 dict 字段按空 section 处理。
def _section(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# LLM: _event_type normalizes event type values for exact dispatch.
# 函数用途: 读取 type 字段并转小写字符串。
def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


# LLM: _status normalizes lifecycle-like status values.
# 函数用途: 把状态字段转为大写字符串。
def _status(value: object) -> str:
    return _text(value).upper()


# LLM: _string_list normalizes list-like fields without parsing embedded prose.
# 函数用途: 把结构化数组规整成去空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in [_text(item)] if text]


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineCompactResumeValidation", "validate_compact_resume_bundle"]
