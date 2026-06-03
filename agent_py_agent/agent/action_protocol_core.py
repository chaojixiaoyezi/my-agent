
from __future__ import annotations

"""Core refs for the typed action protocol.

这些类型不执行任何动作，只负责让工具、子代理、compact 恢复包都能带稳定的
scope 和 refs。自然语言 summary 不会在这里被解析成事实。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .common.value_parsing import string_list

ACTION_PROTOCOL_SCHEMA_VERSION = 1
UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_operation_id(kind: str, identifier: str) -> str:
    clean_kind = str(kind or "operation").strip() or "operation"
    clean_identifier = str(identifier or "unknown").strip() or "unknown"
    return f"{clean_kind}:{clean_identifier}"


@dataclass(frozen=True)
class RunScope:
    request_id: str = ""
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
    reserved: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RunScope:
        data = payload if isinstance(payload, dict) else {}
        return cls(
            request_id=str(data.get("request_id") or ""),
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
            reserved=_dict_or_empty(data.get("reserved")),
        )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    path: str
    kind: str = "file"
    owner_run_id: str = ""
    hash: str = ""
    summary: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactRef:
        return cls(
            artifact_id=str(payload.get("artifact_id") or payload.get("id") or ""),
            path=str(payload.get("path") or ""),
            kind=str(payload.get("kind") or "file"),
            owner_run_id=str(payload.get("owner_run_id") or ""),
            hash=str(payload.get("hash") or ""),
            summary=str(payload.get("summary") or ""),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


@dataclass(frozen=True)
class PathRef:
    path: str
    kind: str = "file"
    owner_run_id: str = ""
    source: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PathRef:
        return cls(
            path=str(payload.get("path") or ""),
            kind=str(payload.get("kind") or "file"),
            owner_run_id=str(payload.get("owner_run_id") or ""),
            source=str(payload.get("source") or ""),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    claim: str
    checked_scope: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reserved: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceRef:
        return cls(
            evidence_id=str(payload.get("evidence_id") or payload.get("id") or ""),
            claim=str(payload.get("claim") or ""),
            checked_scope=str(payload.get("checked_scope") or ""),
            evidence_refs=string_list(payload.get("evidence_refs")),
            artifact_refs=string_list(payload.get("artifact_refs")),
            confidence=_float_or_zero(payload.get("confidence")),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


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
