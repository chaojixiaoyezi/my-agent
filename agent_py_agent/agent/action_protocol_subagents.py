
from __future__ import annotations

"""Subagent protocol envelope layer."""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    ArtifactRef,
    EvidenceRef,
    PathRef,
    RunScope,
    _default_operation_id,
    _dict_list,
    _dict_or_empty,
    _now_iso,
)
from .common.value_parsing import string_list


@dataclass(frozen=True)
class SubagentResultEnvelope:
    result_id: str
    run_id: str
    status: str
    summary: str = ""
    actual_tools: list[str] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    path_refs: list[PathRef] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    failure_type: str = ""
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "subagent_result"
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.result_id))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        payload["artifact_refs"] = [item.to_dict() for item in self.artifact_refs]
        payload["evidence_refs"] = [item.to_dict() for item in self.evidence_refs]
        payload["path_refs"] = [item.to_dict() for item in self.path_refs]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubagentResultEnvelope:
        return cls(
            result_id=str(payload.get("result_id") or payload.get("id") or ""),
            run_id=str(payload.get("run_id") or ""),
            status=str(payload.get("status") or ""),
            summary=str(payload.get("summary") or ""),
            actual_tools=string_list(payload.get("actual_tools")),
            artifact_refs=[ArtifactRef.from_dict(item) for item in _dict_list(payload.get("artifact_refs"))],
            evidence_refs=[EvidenceRef.from_dict(item) for item in _dict_list(payload.get("evidence_refs"))],
            path_refs=[PathRef.from_dict(item) for item in _dict_list(payload.get("path_refs"))],
            tests=_dict_list(payload.get("tests")),
            next_actions=string_list(payload.get("next_actions")),
            blocked_reason=str(payload.get("blocked_reason") or ""),
            failure_type=str(payload.get("failure_type") or ""),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "subagent_result"),
            created_at=str(payload.get("created_at") or _now_iso()),
        )


@dataclass(frozen=True)
class SubagentScheduleEnvelope:
    schedule_id: str
    tool: str
    created_run_ids: list[str] = field(default_factory=list)
    reused_run_ids: list[str] = field(default_factory=list)
    dispatch_run_ids: list[str] = field(default_factory=list)
    parent_run_id: str = ""
    root_id: str = ""
    planned_count: int = 0
    next_action: Any = field(default_factory=dict)
    current_turn_run_state: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    blocked: bool = False
    reason: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "subagent_schedule"
    created_at: str = field(default_factory=_now_iso)
    source: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.schedule_id))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubagentScheduleEnvelope:
        return cls(
            schedule_id=str(payload.get("schedule_id") or payload.get("id") or ""),
            tool=str(payload.get("tool") or ""),
            created_run_ids=string_list(payload.get("created_run_ids")),
            reused_run_ids=string_list(payload.get("reused_run_ids")),
            dispatch_run_ids=string_list(payload.get("dispatch_run_ids")),
            parent_run_id=str(payload.get("parent_run_id") or ""),
            root_id=str(payload.get("root_id") or ""),
            planned_count=int(payload.get("planned_count") or 0),
            next_action=_jsonish_value(payload.get("next_action"), default={}),
            current_turn_run_state=_dict_or_empty(payload.get("current_turn_run_state")),
            dry_run=bool(payload.get("dry_run")),
            blocked=bool(payload.get("blocked")),
            reason=str(payload.get("reason") or ""),
            items=_dict_list(payload.get("items")),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "subagent_schedule"),
            created_at=str(payload.get("created_at") or _now_iso()),
            source=str(payload.get("source") or ""),
        )


def path_refs_from_subagent_refs(
    *,
    artifact_refs: list[ArtifactRef],
    evidence_refs: list[EvidenceRef],
    owner_run_id: str = "",
) -> list[PathRef]:
    refs: list[PathRef] = []
    seen: set[str] = set()

    def add(path: str, kind: str, source: str, owner: str = "") -> None:
        clean = str(path or "").strip()
        if not clean or clean in seen:
            return
        seen.add(clean)
        refs.append(PathRef(path=clean, kind=kind or "file", owner_run_id=owner, source=source))

    for item in artifact_refs:
        add(item.path, item.kind, "artifact_ref", item.owner_run_id or owner_run_id)
    for item in evidence_refs:
        for ref in item.artifact_refs:
            add(ref, "artifact", f"evidence:{item.evidence_id}:artifact_refs", owner_run_id)
        for ref in item.evidence_refs:
            add(ref, "evidence", f"evidence:{item.evidence_id}:evidence_refs", owner_run_id)
    return refs


def subagent_schedule_envelope_from_payload(
    payload: dict[str, Any],
    *,
    tool: str,
    scope: RunScope | None = None,
) -> SubagentScheduleEnvelope:
    created_run_ids = _schedule_created_run_ids(payload)
    parent_run_id = str(payload.get("parent_run_id") or "")
    root_id = str(payload.get("root_id") or "")
    return SubagentScheduleEnvelope(
        schedule_id=_schedule_envelope_id(tool, created_run_ids, parent_run_id, root_id),
        tool=tool,
        created_run_ids=created_run_ids,
        reused_run_ids=string_list(payload.get("reused_run_ids")),
        dispatch_run_ids=string_list(payload.get("dispatch_run_ids")),
        parent_run_id=parent_run_id,
        root_id=root_id,
        planned_count=int(payload.get("planned_count") or len(created_run_ids)),
        next_action=_jsonish_value(payload.get("next_action"), default={}),
        current_turn_run_state=_dict_or_empty(payload.get("current_turn_run_state")),
        dry_run=bool(payload.get("dry_run")),
        blocked=bool(payload.get("blocked")),
        reason=str(payload.get("reason") or ""),
        items=_dict_list(payload.get("items") or payload.get("tasks")),
        scope=scope or RunScope(run_id=parent_run_id, root_task_id=root_id),
        source="subagent_orchestration_tool",
    )


def _jsonish_value(value: object, *, default: object) -> object:
    if isinstance(value, str | dict | list | int | float | bool):
        return value
    return default


def _schedule_created_run_ids(payload: dict[str, Any]) -> list[str]:
    ids = string_list(payload.get("created_run_ids") or payload.get("ids"))
    if ids:
        return ids
    return [
        str(item.get("id") or item.get("run_id"))
        for item in _dict_list(payload.get("tasks") or payload.get("items"))
        if str(item.get("id") or item.get("run_id") or "").strip()
    ]


def _schedule_envelope_id(tool: str, run_ids: list[str], parent_run_id: str, root_id: str) -> str:
    anchor = run_ids[0] if run_ids else parent_run_id or root_id or "unknown"
    return f"{tool}:{anchor}"
