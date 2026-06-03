
from __future__ import annotations

"""Subagent protocol contracts.

`CreateRunParams` 适合代码内部调用，但父子代理之间更需要稳定“线协议”。
这里的 TaskAddress / TaskEnvelope 是第一版协议包：先把地址、工具、写入、验收和上下文 refs
整理成机器字段，后续 dispatch、recovery、QA 都可以读同一种结构。
"""

from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field

from ..model_visible_refs import current_model_ref, current_model_ref_list, current_model_text
from .models import SubAgentTask
from .protocol_write_contract import build_write_contract


@dataclass(frozen=True)
class ProtocolIssue:

    kind: str
    code: str
    field: str = ""
    message: str = ""
    severity: str = "error"
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProtocolValidationReport:

    ok: bool
    issues: list[ProtocolIssue] = dataclass_field(default_factory=list)
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "reserved": dict(self.reserved),
        }


@dataclass(frozen=True)
class TaskAddress:

    schema_version: str
    run_id: str
    root_id: str
    parent_id: str = ""
    depth: int = 0
    lineage: list[str] = dataclass_field(default_factory=list)
    attempt_id: str = ""
    workspace_ref: str = ""
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TaskEnvelope:

    schema_version: str
    address: TaskAddress
    goal: str
    role: str
    display_name: str = ""
    plan: list[str] = dataclass_field(default_factory=list)
    tool_contract: dict[str, object] = dataclass_field(default_factory=dict)
    write_contract: dict[str, object] = dataclass_field(default_factory=dict)
    acceptance: dict[str, object] = dataclass_field(default_factory=dict)
    context_refs: dict[str, object] = dataclass_field(default_factory=dict)
    audit: dict[str, object] = dataclass_field(default_factory=dict)
    reserved: dict[str, object] = dataclass_field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["address"] = self.address.to_dict()
        return payload


def build_task_address(task: SubAgentTask, *, all_tasks: list[SubAgentTask] | None = None) -> TaskAddress:
    run_id = _task_text(task, "id")
    return TaskAddress(
        schema_version="subagent_task_address.v1",
        run_id=run_id,
        root_id=_task_text(task, "root_id") or run_id,
        parent_id=_task_text(task, "parent_id"),
        depth=_task_int(task, "depth"),
        lineage=_lineage_for_task(task, all_tasks or []),
        attempt_id=_task_text(task, "runner_active_attempt_id") or _attempt_from_task(task),
        workspace_ref=_workspace_ref(task),
        reserved={"session_id": _task_text(task, "subagent_session_id"), "thread_id": _task_text(task, "agent_thread_id")},
    )


def build_task_envelope(task: SubAgentTask, *, all_tasks: list[SubAgentTask] | None = None) -> TaskEnvelope:
    return TaskEnvelope(
        schema_version="subagent_task_envelope.v1",
        address=build_task_address(task, all_tasks=all_tasks),
        goal=current_model_text(_task_text(task, "goal")),
        role=_task_text(task, "role"),
        display_name=_task_text(task, "agent_name"),
        plan=[current_model_text(item) for item in _task_list(task, "plan")],
        tool_contract=_tool_contract(task),
        write_contract=build_write_contract(task),
        acceptance=_acceptance_contract(task),
        context_refs=_context_refs(task),
        audit={"created_at": _task_float(task, "created_at"), "updated_at": _task_float(task, "updated_at"), "contract_version": "v1"},
    )


def validate_task_envelope(envelope: TaskEnvelope) -> ProtocolValidationReport:
    issues: list[ProtocolIssue] = []
    if not envelope.goal.strip():
        issues.append(_protocol_issue("missing_goal", "goal", "TaskEnvelope.goal is required."))
    if not list(envelope.acceptance.get("checks") or []):
        issues.append(
            _protocol_issue(
                "missing_acceptance_checks",
                "acceptance.checks",
                "TaskEnvelope.acceptance.checks must include at least one caller-visible check.",
            )
        )
    return ProtocolValidationReport(ok=not issues, issues=issues)


def task_envelope_dict(task: SubAgentTask, *, all_tasks: list[SubAgentTask] | None = None) -> dict[str, object]:
    return build_task_envelope(task, all_tasks=all_tasks).to_dict()


def _protocol_issue(code: str, field: str, message: str) -> ProtocolIssue:
    return ProtocolIssue(kind="ProtocolError", code=code, field=field, message=message)


def _task_text(task: object, name: str) -> str:
    return str(getattr(task, name, "") or "")


def _task_list(task: object, name: str) -> list:
    value = getattr(task, name, [])
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _task_int(task: object, name: str) -> int:
    try:
        return int(getattr(task, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _task_float(task: object, name: str) -> float:
    try:
        return float(getattr(task, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _lineage_for_task(task: SubAgentTask, all_tasks: list[SubAgentTask]) -> list[str]:
    task_id = _task_text(task, "id")
    by_id = {_task_text(item, "id"): item for item in all_tasks if _task_text(item, "id")}
    lineage = [task_id] if task_id else []
    current = task
    seen = {task_id}
    while _task_text(current, "parent_id") and _task_text(current, "parent_id") in by_id and _task_text(current, "parent_id") not in seen:
        parent_id = _task_text(current, "parent_id")
        current = by_id[parent_id]
        lineage.append(_task_text(current, "id"))
        seen.add(parent_id)
    lineage.reverse()
    return lineage


def _attempt_from_task(task: SubAgentTask) -> str:
    attempts = _task_int(task, "runner_attempts")
    return f"{_task_text(task, 'id')}:attempt-{attempts}"


def _workspace_ref(task: SubAgentTask) -> str:
    for field in ("agent_run_workspace_dir", "task_workspace_dir", "task_dir"):
        if ref := current_model_ref(_task_text(task, field)):
            return ref
    return ""


def _tool_contract(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_tools": _task_list(task, "allowed_tools"),
        "used_tools": _task_list(task, "used_tools"),
        "controlled_exec_grant_ids": [
            grant.id for grant in _task_list(task, "capability_grants") if "controlled_exec" in list(getattr(grant, "tools", []) or [])
        ],
        "open_request_count": len([item for item in _task_list(task, "capability_requests") if _request_is_open(item)]),
        "grant_count": len(_task_list(task, "capability_grants")),
        "gap_count": len(_task_list(task, "capability_gaps")),
    }


def _acceptance_contract(task: SubAgentTask) -> dict[str, object]:
    return {
        "checks": [current_model_text(item) for item in _task_list(task, "acceptance_checks")],
        "required_outputs": current_model_ref_list(_task_list(task, "artifact_refs"), basename_for_legacy=True),
    }


def _context_refs(task: SubAgentTask) -> dict[str, object]:
    refs = {
        "checkpoint": current_model_ref(_task_text(task, "agent_run_checkpoint_json") or _task_text(task, "checkpoint_ref")),
        "summary": current_model_ref(_task_text(task, "agent_run_summary_md")),
        "context_bundle": _first_context_bundle_ref(task),
        "final_report": current_model_ref(_task_text(task, "agent_run_final_report_md")),
        "task_dir": current_model_ref(_task_text(task, "task_dir")),
    }
    return {key: value for key, value in refs.items() if value}


def _first_context_bundle_ref(task: SubAgentTask) -> str:
    for item in _task_list(task, "context_packs"):
        ref = _context_pack_ref(item)
        if ref:
            return ref
    return ""


def _context_pack_ref(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    return current_model_ref(item.get("context_bundle_json") or item.get("ref") or "")


def _request_is_open(item: object) -> bool:
    return str(getattr(item, "status", "OPEN") or "OPEN").upper() not in {
        "CLOSED",
        "RESOLVED",
        "REJECTED",
        "APPROVED",
        "GRANTED",
    }


__all__ = [
    "ProtocolIssue",
    "ProtocolValidationReport",
    "TaskAddress",
    "TaskEnvelope",
    "build_task_address",
    "build_task_envelope",
    "task_envelope_dict",
    "validate_task_envelope",
]
