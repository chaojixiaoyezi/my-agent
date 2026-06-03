
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .model_helpers import (
    _float,
    _tuple_of_dicts,
    _tuple_of_strings,
    new_case_id,
    new_decision_id,
    new_evidence_id,
    new_request_id,
)

SCHEMA_VERSION = "collaboration.v1"


@dataclass(frozen=True)
class AgentCapability:
    agent_id: str
    role: str = ""
    capabilities: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    status: str = "available"
    load: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["capabilities"] = list(self.capabilities)
        payload["sources"] = list(self.sources)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCapability:
        metadata = data.get("metadata")
        return cls(
            agent_id=str(data.get("agent_id") or ""),
            role=str(data.get("role") or ""),
            capabilities=_tuple_of_strings(data.get("capabilities")),
            sources=_tuple_of_strings(data.get("sources")),
            status=str(data.get("status") or "available"),
            load=_float(data.get("load")),
            updated_at=_float(data.get("updated_at")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class CollaborationCase:
    case_id: str
    thread_id: str
    task_id: str = ""
    title: str = ""
    summary: str = ""
    status: str = "open"
    priority: str = "normal"
    created_by: str = ""
    required_capabilities: tuple[str, ...] = ()
    entities: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["required_capabilities"] = list(self.required_capabilities)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollaborationCase:
        metadata = data.get("metadata")
        entities = data.get("entities")
        return cls(
            case_id=str(data.get("case_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            task_id=str(data.get("task_id") or ""),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            status=str(data.get("status") or "open"),
            priority=str(data.get("priority") or "normal"),
            created_by=str(data.get("created_by") or ""),
            required_capabilities=_tuple_of_strings(data.get("required_capabilities")),
            entities=entities if isinstance(entities, dict) else {},
            created_at=_float(data.get("created_at")),
            updated_at=_float(data.get("updated_at")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class CaseParticipant:
    case_id: str
    agent_id: str
    role: str = ""
    status: str = "requested"
    requested_capabilities: tuple[str, ...] = ()
    joined_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["requested_capabilities"] = list(self.requested_capabilities)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaseParticipant:
        metadata = data.get("metadata")
        return cls(
            case_id=str(data.get("case_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            role=str(data.get("role") or ""),
            status=str(data.get("status") or "requested"),
            requested_capabilities=_tuple_of_strings(data.get("requested_capabilities")),
            joined_at=_float(data.get("joined_at")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class CollaborationRequest:
    request_id: str
    case_id: str
    requester_agent_id: str
    target_agent_ids: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    question: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    problem_statement: str = ""
    observed_facts: tuple[dict[str, Any], ...] = ()
    query_intent: dict[str, Any] = field(default_factory=dict)
    query_hints: tuple[dict[str, Any], ...] = ()
    routing_requirements: dict[str, Any] = field(default_factory=dict)
    response_contract: dict[str, Any] = field(default_factory=dict)
    context_refs: tuple[str, ...] = ()
    priority: str = "normal"
    deadline_at: float = 0.0
    status: str = "open"
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["target_agent_ids"] = list(self.target_agent_ids)
        payload["required_capabilities"] = list(self.required_capabilities)
        payload["observed_facts"] = list(self.observed_facts)
        payload["query_hints"] = list(self.query_hints)
        payload["context_refs"] = list(self.context_refs)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollaborationRequest:
        metadata = data.get("metadata")
        entities = data.get("entities")
        query_intent = data.get("query_intent")
        routing_requirements = data.get("routing_requirements")
        response_contract = data.get("response_contract")
        return cls(
            request_id=str(data.get("request_id") or ""),
            case_id=str(data.get("case_id") or ""),
            requester_agent_id=str(data.get("requester_agent_id") or ""),
            target_agent_ids=_tuple_of_strings(data.get("target_agent_ids")),
            required_capabilities=_tuple_of_strings(data.get("required_capabilities")),
            question=str(data.get("question") or ""),
            entities=entities if isinstance(entities, dict) else {},
            problem_statement=str(data.get("problem_statement") or ""),
            observed_facts=_tuple_of_dicts(data.get("observed_facts")),
            query_intent=query_intent if isinstance(query_intent, dict) else {},
            query_hints=_tuple_of_dicts(data.get("query_hints")),
            routing_requirements=routing_requirements if isinstance(routing_requirements, dict) else {},
            response_contract=response_contract if isinstance(response_contract, dict) else {},
            context_refs=_tuple_of_strings(data.get("context_refs")),
            priority=str(data.get("priority") or "normal"),
            deadline_at=_float(data.get("deadline_at")),
            status=str(data.get("status") or "open"),
            created_at=_float(data.get("created_at")),
            updated_at=_float(data.get("updated_at") or data.get("created_at")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class EvidencePacket:
    evidence_id: str
    case_id: str
    request_id: str = ""
    source_agent_id: str = ""
    matched: bool = False
    summary: str = ""
    evidence_refs: tuple[str, ...] = ()
    queried_scopes: tuple[str, ...] = ()
    used_query_hints: tuple[str, ...] = ()
    miss_reason: str = ""
    response_facts: tuple[dict[str, Any], ...] = ()
    followup_suggestions: tuple[dict[str, Any], ...] = ()
    query_actions: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0
    limitations: tuple[str, ...] = ()
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["queried_scopes"] = list(self.queried_scopes)
        payload["used_query_hints"] = list(self.used_query_hints)
        payload["response_facts"] = list(self.response_facts)
        payload["followup_suggestions"] = list(self.followup_suggestions)
        payload["query_actions"] = list(self.query_actions)
        payload["limitations"] = list(self.limitations)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidencePacket:
        metadata = data.get("metadata")
        return cls(
            evidence_id=str(data.get("evidence_id") or ""),
            case_id=str(data.get("case_id") or ""),
            request_id=str(data.get("request_id") or ""),
            source_agent_id=str(data.get("source_agent_id") or ""),
            matched=bool(data.get("matched", False)),
            summary=str(data.get("summary") or ""),
            evidence_refs=_tuple_of_strings(data.get("evidence_refs")),
            queried_scopes=_tuple_of_strings(data.get("queried_scopes")),
            used_query_hints=_tuple_of_strings(data.get("used_query_hints")),
            miss_reason=str(data.get("miss_reason") or ""),
            response_facts=_tuple_of_dicts(data.get("response_facts")),
            followup_suggestions=_tuple_of_dicts(data.get("followup_suggestions")),
            query_actions=_tuple_of_dicts(data.get("query_actions")),
            confidence=_float(data.get("confidence")),
            limitations=_tuple_of_strings(data.get("limitations")),
            created_at=_float(data.get("created_at")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True)
class CaseDecision:
    decision_id: str
    case_id: str
    decision_type: str = "observation"
    summary: str = ""
    requires_main_agent: bool = False
    wake_signal_id: str = ""
    evidence_ids: tuple[str, ...] = ()
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = SCHEMA_VERSION
        payload["evidence_ids"] = list(self.evidence_ids)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaseDecision:
        metadata = data.get("metadata")
        return cls(
            decision_id=str(data.get("decision_id") or ""),
            case_id=str(data.get("case_id") or ""),
            decision_type=str(data.get("decision_type") or "observation"),
            summary=str(data.get("summary") or ""),
            requires_main_agent=bool(data.get("requires_main_agent", False)),
            wake_signal_id=str(data.get("wake_signal_id") or ""),
            evidence_ids=_tuple_of_strings(data.get("evidence_ids")),
            created_at=_float(data.get("created_at")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


__all__ = [
    "AgentCapability",
    "CaseDecision",
    "CaseParticipant",
    "CollaborationCase",
    "CollaborationRequest",
    "EvidencePacket",
    "SCHEMA_VERSION",
    "new_case_id",
    "new_decision_id",
    "new_evidence_id",
    "new_request_id",
]
