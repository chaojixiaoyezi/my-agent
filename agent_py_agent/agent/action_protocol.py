"""Typed action protocol for shared refs, subagents, and compaction packets.

Was split across action_protocol_core / action_protocol_tooling /
action_protocol_subagents / action_protocol_subagent_dispatch — now merged.
工具调用只使用 tooling.runtime_contracts 中的 ToolCall/ToolResult；
自然语言回复不在这里获得执行权。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .common.value_parsing import string_list

ACTION_PROTOCOL_SCHEMA_VERSION = 1
UTC = timezone.utc


# ===========================================================================
# core helpers (was action_protocol_core.py)
# ===========================================================================

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_operation_id(kind: str, identifier: str) -> str:
    clean_kind = str(kind or "operation").strip() or "operation"
    clean_identifier = str(identifier or "unknown").strip() or "unknown"
    return f"{clean_kind}:{clean_identifier}"


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _float_or_zero(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _jsonish_value(value: object, *, default: object) -> object:
    if isinstance(value, str | dict | list | int | float | bool):
        return value
    return default


# ===========================================================================
# core types (was action_protocol_core.py)
# ===========================================================================

@dataclass(frozen=True)
class RunScope:
    request_id: str = ""
    # One runtime invocation can continue the same durable run/task.  Provider
    # tool-call ids are only unique inside that invocation, so operation
    # identity must include this host-generated attempt instead of assuming a
    # call id is globally unique for the whole long-running task.
    attempt_id: str = ""
    session_id: str = ""
    task_id: str = ""
    run_id: str = ""
    owner_type: str = ""
    owner_id: str = ""
    parent_run_id: str = ""
    root_task_id: str = ""
    root_run_id: str = ""
    depth: int = 0
    agent_kind: str = ""
    task_load_error: dict[str, Any] = field(default_factory=dict)
    delivery_evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RunScope:
        data = payload if isinstance(payload, dict) else {}
        return cls(
            request_id=str(data.get("request_id") or ""),
            attempt_id=str(data.get("attempt_id") or ""),
            session_id=str(data.get("session_id") or ""),
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            owner_type=str(data.get("owner_type") or ""),
            owner_id=str(data.get("owner_id") or ""),
            parent_run_id=str(data.get("parent_run_id") or ""),
            root_task_id=str(data.get("root_task_id") or ""),
            root_run_id=str(data.get("root_run_id") or ""),
            depth=_int_or_zero(data.get("depth")),
            agent_kind=str(data.get("agent_kind") or ""),
            task_load_error=_dict_or_empty(data.get("task_load_error")),
            delivery_evidence_refs=tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in (
                        data.get("delivery_evidence_refs")
                        if isinstance(data.get("delivery_evidence_refs"), (list, tuple))
                        else ()
                    )
                    if str(item).strip()
                )
            ),
        )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    path: str
    kind: str = "file"
    owner_run_id: str = ""
    hash: str = ""
    summary: str = ""
    size_bytes: int = 0
    mime_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactRef:
        return cls(
            artifact_id=str(payload.get("artifact_id") or ""),
            path=str(payload.get("path") or ""),
            kind=str(payload.get("kind") or "file"),
            owner_run_id=str(payload.get("owner_run_id") or ""),
            hash=str(payload.get("hash") or ""),
            summary=str(payload.get("summary") or ""),
            size_bytes=_int_or_zero(payload.get("size_bytes")),
            mime_type=str(payload.get("mime_type") or ""),
        )


@dataclass(frozen=True)
class PathRef:
    path: str
    kind: str = "file"
    owner_run_id: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PathRef:
        return cls(
            path=str(payload.get("path") or ""),
            kind=str(payload.get("kind") or "file"),
            owner_run_id=str(payload.get("owner_run_id") or ""),
            source=str(payload.get("source") or ""),
        )


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    claim: str
    checked_scope: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceRef:
        return cls(
            evidence_id=str(payload.get("evidence_id") or ""),
            claim=str(payload.get("claim") or ""),
            checked_scope=str(payload.get("checked_scope") or ""),
            evidence_refs=string_list(payload.get("evidence_refs")),
            artifact_refs=string_list(payload.get("artifact_refs")),
            confidence=_float_or_zero(payload.get("confidence")),
        )


# ===========================================================================
# subagent envelopes (was action_protocol_subagents.py)
# ===========================================================================

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
            result_id=str(payload.get("result_id") or ""),
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
    accepted_run_ids: list[str] = field(default_factory=list)
    running_run_ids: list[str] = field(default_factory=list)
    failed_run_ids: list[str] = field(default_factory=list)
    requested_count: int = 0
    acceptance_status: str = ""
    lifecycle_counts: dict[str, int] = field(default_factory=dict)
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
            schedule_id=str(payload.get("schedule_id") or ""),
            tool=str(payload.get("tool") or ""),
            created_run_ids=string_list(payload.get("created_run_ids")),
            reused_run_ids=string_list(payload.get("reused_run_ids")),
            dispatch_run_ids=string_list(payload.get("dispatch_run_ids")),
            accepted_run_ids=string_list(payload.get("accepted_run_ids")),
            running_run_ids=string_list(payload.get("running_run_ids")),
            failed_run_ids=string_list(payload.get("failed_run_ids")),
            requested_count=_int_or_zero(payload.get("requested_count")),
            acceptance_status=str(payload.get("acceptance_status") or ""),
            lifecycle_counts={
                str(key): _int_or_zero(value)
                for key, value in _dict_or_empty(payload.get("lifecycle_counts")).items()
            },
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


def _schedule_created_run_ids(payload: dict[str, Any]) -> list[str]:
    return string_list(payload.get("created_run_ids"))


def _schedule_envelope_id(tool: str, run_ids: list[str], parent_run_id: str, root_id: str) -> str:
    anchor = run_ids[0] if run_ids else parent_run_id or root_id or "unknown"
    return f"{tool}:{anchor}"


def subagent_schedule_envelope_from_payload(
    payload: dict[str, Any],
    *,
    tool: str,
    scope: RunScope | None = None,
) -> SubagentScheduleEnvelope:
    created_run_ids = _schedule_created_run_ids(payload)
    parent_run_id = str(payload.get("parent_run_id") or "")
    root_id = str(payload.get("root_id") or "")
    lifecycle = _dict_or_empty(payload.get("schedule_lifecycle"))
    return SubagentScheduleEnvelope(
        schedule_id=_schedule_envelope_id(tool, created_run_ids, parent_run_id, root_id),
        tool=tool,
        created_run_ids=created_run_ids,
        reused_run_ids=string_list(payload.get("reused_run_ids")),
        dispatch_run_ids=string_list(payload.get("dispatch_run_ids")),
        accepted_run_ids=string_list(lifecycle.get("accepted_run_ids")),
        running_run_ids=string_list(lifecycle.get("running_run_ids")),
        failed_run_ids=string_list(lifecycle.get("failed_run_ids")),
        requested_count=_int_or_zero(lifecycle.get("requested_count")),
        acceptance_status=str(lifecycle.get("acceptance_status") or ""),
        lifecycle_counts={
            str(key): _int_or_zero(value)
            for key, value in _dict_or_empty(lifecycle.get("counts")).items()
        },
        parent_run_id=parent_run_id,
        root_id=root_id,
        planned_count=int(payload.get("planned_count") or len(created_run_ids)),
        next_action=_jsonish_value(payload.get("next_action"), default={}),
        current_turn_run_state=_dict_or_empty(payload.get("current_turn_run_state")),
        dry_run=bool(payload.get("dry_run")),
        blocked=bool(payload.get("blocked")),
        reason=str(payload.get("reason") or ""),
        items=_dict_list(payload.get("items")),
        scope=scope or RunScope(run_id=parent_run_id, root_task_id=root_id),
        source="subagent_orchestration_tool",
    )


# ===========================================================================
# dispatch envelope (was action_protocol_subagent_dispatch.py)
# ===========================================================================

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


# ===========================================================================
# compact continue packet (was original action_protocol.py)
# ===========================================================================

@dataclass(frozen=True)
class CompactContinuePacketEnvelope:
    packet_id: str
    apply_id: str
    plan_id: str
    ready_to_continue: bool
    continue_mode: str
    owner: dict[str, Any] = field(default_factory=dict)
    work_state: dict[str, Any] = field(default_factory=dict)
    guard: dict[str, Any] = field(default_factory=dict)
    path_refs: list[PathRef] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    scope: RunScope = field(default_factory=RunScope)
    operation_id: str = ""
    schema_version: int = ACTION_PROTOCOL_SCHEMA_VERSION
    kind: str = "compact_continue_packet"
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", _default_operation_id(self.kind, self.packet_id))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = self.scope.to_dict()
        payload["path_refs"] = [item.to_dict() for item in self.path_refs]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompactContinuePacketEnvelope:
        return cls(
            packet_id=str(payload.get("packet_id") or ""),
            apply_id=str(payload.get("apply_id") or ""),
            plan_id=str(payload.get("plan_id") or ""),
            ready_to_continue=bool(payload.get("ready_to_continue")),
            continue_mode=str(payload.get("continue_mode") or ""),
            owner=_dict_or_empty(payload.get("owner")),
            work_state=_dict_or_empty(payload.get("work_state")),
            guard=_dict_or_empty(payload.get("guard")),
            path_refs=[PathRef.from_dict(item) for item in _dict_list(payload.get("path_refs"))],
            next_actions=string_list(payload.get("next_actions")),
            scope=RunScope.from_dict(payload.get("scope")),
            operation_id=str(payload.get("operation_id") or ""),
            schema_version=int(payload.get("schema_version") or ACTION_PROTOCOL_SCHEMA_VERSION),
            kind=str(payload.get("kind") or "compact_continue_packet"),
            created_at=str(payload.get("created_at") or _now_iso()),
        )


# ===========================================================================
# dispatch function (was original action_protocol.py)
# ===========================================================================

def decode_action_envelope(
    payload: dict[str, Any],
) -> (
    SubagentResultEnvelope
    | CompactContinuePacketEnvelope
    | SubagentScheduleEnvelope
    | SubagentDispatchEnvelope
):
    kind = str(payload.get("kind") or "")
    if kind == "subagent_result":
        return SubagentResultEnvelope.from_dict(payload)
    if kind == "compact_continue_packet":
        return CompactContinuePacketEnvelope.from_dict(payload)
    if kind == "subagent_schedule":
        return SubagentScheduleEnvelope.from_dict(payload)
    if kind == "subagent_dispatch":
        return SubagentDispatchEnvelope.from_dict(payload)
    raise ValueError(f"Unknown action envelope kind: {kind or '<missing>'}")


__all__ = [
    "ACTION_PROTOCOL_SCHEMA_VERSION",
    "ArtifactRef",
    "CompactContinuePacketEnvelope",
    "EvidenceRef",
    "PathRef",
    "RunScope",
    "SubagentDispatchEnvelope",
    "SubagentResultEnvelope",
    "SubagentScheduleEnvelope",
    "decode_action_envelope",
    "path_refs_from_subagent_refs",
    "subagent_dispatch_envelope_from_payload",
    "subagent_schedule_envelope_from_payload",
]
