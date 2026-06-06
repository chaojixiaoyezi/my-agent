
from __future__ import annotations

"""Typed dispatch envelope for subagent orchestration."""

from dataclasses import asdict, dataclass, field
from typing import Any

from .action_protocol_core import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    RunScope,
    _default_operation_id,
    _dict_list,
    _dict_or_empty,
    _now_iso,
)
from .common.value_parsing import string_list


@dataclass(frozen=True)
class SubagentDispatchEnvelope:
    dispatch_id: str
    tool: str = "dispatch_subagents"
    dry_run: bool = False
    summary: dict[str, Any] = field(default_factory=dict)
    actionable_run_ids: list[str] = field(default_factory=list)
    recovery_run_ids: list[str] = field(default_factory=list)
    blocking_run_ids: list[str] = field(default_factory=list)
    unfinished_run_ids: list[str] = field(default_factory=list)
    pending_artifact_refs: list[str] = field(default_factory=list)
    pending_evidence_refs: list[str] = field(default_factory=list)
    deliverable_artifact_refs: list[str] = field(default_factory=list)
    deliverable_evidence_refs: list[str] = field(default_factory=list)
    completion_status: dict[str, Any] = field(default_factory=dict)
    current_turn_run_state: dict[str, Any] = field(default_factory=dict)
    next_action: str = ""
    dispatch_json: str = ""
    dispatch_md: str = ""
    record_count: int = 0
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "subagent_dispatch"
    created_at: str = field(default_factory=_now_iso)
    source: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.dispatch_id))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SubagentDispatchEnvelope:
        return cls(
            dispatch_id=str(payload.get("dispatch_id") or ""),
            tool=str(payload.get("tool") or "dispatch_subagents"),
            dry_run=bool(payload.get("dry_run")),
            summary=dict(payload.get("summary") or {}),
            actionable_run_ids=string_list(payload.get("actionable_run_ids")),
            recovery_run_ids=string_list(payload.get("recovery_run_ids")),
            blocking_run_ids=string_list(payload.get("blocking_run_ids")),
            unfinished_run_ids=string_list(payload.get("unfinished_run_ids")),
            pending_artifact_refs=string_list(payload.get("pending_artifact_refs")),
            pending_evidence_refs=string_list(payload.get("pending_evidence_refs")),
            deliverable_artifact_refs=string_list(payload.get("deliverable_artifact_refs")),
            deliverable_evidence_refs=string_list(payload.get("deliverable_evidence_refs")),
            completion_status=_dict_or_empty(payload.get("completion_status")),
            current_turn_run_state=_dict_or_empty(payload.get("current_turn_run_state")),
            next_action=str(payload.get("next_action") or ""),
            dispatch_json=str(payload.get("dispatch_json") or ""),
            dispatch_md=str(payload.get("dispatch_md") or ""),
            record_count=int(payload.get("record_count") or 0),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "subagent_dispatch"),
            created_at=str(payload.get("created_at") or _now_iso()),
            source=str(payload.get("source") or ""),
        )


def subagent_dispatch_envelope_from_payload(
    payload: dict[str, Any],
    *,
    scope: RunScope | None = None,
) -> SubagentDispatchEnvelope:
    return SubagentDispatchEnvelope(
        dispatch_id=_dispatch_envelope_id(payload),
        dry_run=bool(payload.get("dry_run")),
        summary=_dispatch_summary(payload.get("summary")),
        actionable_run_ids=_dispatch_actionable_run_ids(payload),
        recovery_run_ids=_dispatch_recovery_run_ids(payload),
        blocking_run_ids=string_list(payload.get("blocking_run_ids")),
        unfinished_run_ids=string_list(payload.get("unfinished_run_ids")),
        pending_artifact_refs=string_list(payload.get("pending_artifact_refs")),
        pending_evidence_refs=string_list(payload.get("pending_evidence_refs")),
        deliverable_artifact_refs=string_list(payload.get("deliverable_artifact_refs")),
        deliverable_evidence_refs=string_list(payload.get("deliverable_evidence_refs")),
        completion_status=_dict_or_empty(payload.get("completion_status")),
        current_turn_run_state=_dict_or_empty(payload.get("current_turn_run_state")),
        next_action=str(payload.get("next_action") or ""),
        dispatch_json=str(payload.get("dispatch_json") or ""),
        dispatch_md=str(payload.get("dispatch_md") or ""),
        record_count=len(_dict_list(payload.get("records"))),
        scope=scope or RunScope(),
        source="dispatch_subagents_tool",
    )


def _dispatch_actionable_run_ids(payload: dict[str, Any]) -> list[str]:
    direct = payload.get("direct_children") if isinstance(payload.get("direct_children"), dict) else {}
    ids = string_list((direct or {}).get("actionable_run_ids"))
    if ids:
        return ids
    return _record_run_ids(payload)


def _dispatch_summary(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    return {"text": str(value)}


def _dispatch_recovery_run_ids(payload: dict[str, Any]) -> list[str]:
    direct = payload.get("direct_children") if isinstance(payload.get("direct_children"), dict) else {}
    return string_list((direct or {}).get("recovery_run_ids"))


def _record_run_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in _dict_list(payload.get("records")):
        run_id = str(item.get("run_id") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


def _dispatch_envelope_id(payload: dict[str, Any]) -> str:
    anchor = str(payload.get("dispatch_json") or "").strip()
    if not anchor:
        ids = _dispatch_actionable_run_ids(payload) or _record_run_ids(payload)
        anchor = ids[0] if ids else "unknown"
    return f"dispatch_subagents:{anchor}"
