# LLM: Offline concurrency contracts catch duplicate ownership and side-effect races from structured events.
# 模块用途: 校验离线运行事件里的 worker lease、artifact write 和 approval decision 是否存在并发冲突。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TERMINAL_APPROVAL_STATUSES = {"APPROVED", "REJECTED", "EXPIRED", "CANCELLED"}


# LLM: OfflineConcurrencyValidation reports race findings for fake-model and replay tests.
# 类用途: 返回并发合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineConcurrencyValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]


# LLM: validate_concurrency_events checks run ownership and side-effect idempotency without runtime locks.
# 函数用途: 校验 worker lease、artifact 写入和审批决策事件是否存在可离线复现的并发冲突。
def validate_concurrency_events(events: tuple[dict[str, Any], ...]) -> OfflineConcurrencyValidation:
    findings: list[dict[str, str]] = []
    _validate_leases(events, findings)
    _validate_artifact_writes(events, findings)
    _validate_approval_decisions(events, findings)
    return OfflineConcurrencyValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_leases rejects two active workers claiming the same run.
# 函数用途: 按 run_id 记录第一个 lease_claimed 事件，后续不同 worker 视为抢占冲突。
def _validate_leases(events: tuple[dict[str, Any], ...], findings: list[dict[str, str]]) -> None:
    active_by_run: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "lease_claimed" or _inactive(event):
            continue
        run_id = _text(event.get("run_id"))
        worker_id = _text(event.get("worker_id"))
        previous = active_by_run.get(run_id)
        if previous is not None and _text(previous.get("worker_id")) != worker_id:
            findings.append(_lease_conflict(run_id, previous, event))
            continue
        active_by_run[run_id] = event


# LLM: _validate_artifact_writes rejects sibling writes to one artifact unless the operation is idempotent.
# 函数用途: 按 artifact_ref 保存首个写入事件；不同 run 或不同幂等键写同一产物则报冲突。
def _validate_artifact_writes(events: tuple[dict[str, Any], ...], findings: list[dict[str, str]]) -> None:
    writes_by_ref: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "artifact_write":
            continue
        ref = _text(event.get("artifact_ref") or event.get("path"))
        previous = writes_by_ref.get(ref)
        if previous is not None and not _same_artifact_operation(previous, event):
            findings.append(_artifact_conflict(ref, previous, event))
            continue
        writes_by_ref[ref] = event


# LLM: _validate_approval_decisions rejects contradictory terminal decisions for one approval id.
# 函数用途: 同一个 approval_id 首次终态决策成为事实，后续不同终态视为竞态冲突。
def _validate_approval_decisions(events: tuple[dict[str, Any], ...], findings: list[dict[str, str]]) -> None:
    final_by_approval: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "approval_decision":
            continue
        status = _status(event.get("status"))
        if status not in TERMINAL_APPROVAL_STATUSES:
            continue
        approval_id = _text(event.get("approval_id"))
        previous = final_by_approval.get(approval_id)
        if previous is not None and _status(previous.get("status")) != status:
            findings.append(_approval_conflict(approval_id, previous, event))
            continue
        final_by_approval[approval_id] = event


# LLM: _same_artifact_operation defines idempotent artifact write replay from machine fields.
# 函数用途: 判断两次产物写入是否属于同一幂等操作，避免重复模型调用造成误报。
def _same_artifact_operation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    key = _text(left.get("idempotency_key"))
    return bool(key) and key == _text(right.get("idempotency_key"))


# LLM: _lease_conflict builds a compact finding for duplicate worker ownership.
# 函数用途: 生成 LEASE_ALREADY_HELD finding，记录 run 和前后 worker。
def _lease_conflict(run_id: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    return {
        "code": "LEASE_ALREADY_HELD",
        "run_id": run_id,
        "held_by": _text(previous.get("worker_id")),
        "requested_by": _text(current.get("worker_id")),
    }


# LLM: _artifact_conflict builds a compact finding for non-idempotent artifact writes.
# 函数用途: 生成 ARTIFACT_WRITE_CONFLICT finding，记录产物引用和冲突 run。
def _artifact_conflict(ref: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    return {
        "code": "ARTIFACT_WRITE_CONFLICT",
        "artifact_ref": ref,
        "first_run_id": _text(previous.get("run_id")),
        "second_run_id": _text(current.get("run_id")),
    }


# LLM: _approval_conflict builds a compact finding for approval decision races.
# 函数用途: 生成 APPROVAL_DECISION_ALREADY_FINAL finding，记录审批 id 和前后终态。
def _approval_conflict(approval_id: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    return {
        "code": "APPROVAL_DECISION_ALREADY_FINAL",
        "approval_id": approval_id,
        "first_status": _status(previous.get("status")),
        "second_status": _status(current.get("status")),
    }


# LLM: _inactive lets tests represent expired or released leases without deleting events.
# 函数用途: 读取 active/status 结构字段，判断 lease 事件是否不再占用 run。
def _inactive(event: dict[str, Any]) -> bool:
    if event.get("active") is False:
        return True
    return _status(event.get("status")) in {"RELEASED", "EXPIRED", "CANCELLED"}


# LLM: _event_type normalizes event type keys for exact comparisons.
# 函数用途: 读取 type 字段并转小写，避免重复散落 str 转换。
def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


# LLM: _status normalizes lifecycle-like event status values.
# 函数用途: 把状态字段转为大写字符串供终态判断。
def _status(value: object) -> str:
    return _text(value).upper()


# LLM: _text normalizes optional scalar values for contract comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自由文本语义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineConcurrencyValidation", "validate_concurrency_events"]
