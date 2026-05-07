from __future__ import annotations

"""Entity graph projection for findings and cases."""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import CaseRecord, EvidenceRef, Finding


@dataclass
class EntityNode:
    kind: str
    value: str
    evidence_refs: list[str] = field(default_factory=list)

    @property
    def node_id(self) -> str:
        return f"{self.kind}:{self.value}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["node_id"] = self.node_id
        return payload


@dataclass
class EntityEdge:
    source: str
    target: str
    relationship: str
    evidence_refs: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntityEdgeInput:
    # LLM: Edge timing and refs travel as one bundle instead of loose window kwargs.
    source: str
    target: str
    relationship: str
    evidence_refs: Sequence[Any] = ()
    first_seen: str = ""
    last_seen: str = ""


@dataclass
class EntityGraph:
    nodes: dict[str, EntityNode] = field(default_factory=dict)
    edges: list[EntityEdge] = field(default_factory=list)

    def add_node(self, kind: str, value: str, evidence_refs: Sequence[Any] = ()) -> str:
        clean_kind = str(kind or "").strip()
        clean_value = str(value or "").strip()
        if not clean_kind or not clean_value:
            return ""
        node = EntityNode(clean_kind, clean_value)
        existing = self.nodes.get(node.node_id)
        if existing is None:
            existing = node
            self.nodes[node.node_id] = existing
        existing.evidence_refs = _unique([*existing.evidence_refs, *_ref_ids(evidence_refs)])
        return existing.node_id

    def add_edge(
        self,
        source: str | EntityEdgeInput = "",
        target: str = "",
        relationship: str = "",
        *,
        params: EntityEdgeInput | None = None,
        edge: EntityEdgeInput | None = None,
        evidence_refs: Sequence[Any] = (),
        first_seen: str = "",
        last_seen: str = "",
    ) -> None:
        item = params or edge
        if item is None and isinstance(source, EntityEdgeInput):
            item = source
        if item is None:
            item = EntityEdgeInput(
                source=str(source),
                target=str(target),
                relationship=str(relationship),
                evidence_refs=evidence_refs,
                first_seen=str(first_seen),
                last_seen=str(last_seen),
            )
        if not item.source or not item.target or item.source == item.target:
            return
        refs = _ref_ids(item.evidence_refs)
        for edge in self.edges:
            if edge.source == item.source and edge.target == item.target and edge.relationship == item.relationship:
                edge.evidence_refs = _unique([*edge.evidence_refs, *refs])
                edge.first_seen = edge.first_seen or item.first_seen
                edge.last_seen = item.last_seen or edge.last_seen
                return
        self.edges.append(
            EntityEdge(
                source=item.source,
                target=item.target,
                relationship=item.relationship,
                evidence_refs=refs,
                first_seen=item.first_seen,
                last_seen=item.last_seen,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True)
class _EdgeBatch:
    sources: Sequence[str]
    targets: Sequence[str]
    relationship: str
    refs: Sequence[Any]
    first_seen: str = ""
    last_seen: str = ""


@dataclass(frozen=True)
class _NodeBatch:
    kind: str
    values: Sequence[Any]
    refs: Sequence[Any]


@dataclass(frozen=True)
class _FindingEdgeContext:
    nodes_by_kind: dict[str, list[str]]
    refs: Sequence[Any]
    first_seen: str
    last_seen: str


def build_entity_graph(case_or_findings: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> EntityGraph:
    graph = EntityGraph()
    for finding in _extract_findings(case_or_findings):
        refs = finding.evidence_refs
        when_start = finding.window[0] if finding.window else ""
        when_end = finding.window[1] if len(finding.window) > 1 else when_start
        nodes_by_kind = _add_finding_nodes(graph, finding, refs)
        _add_finding_edges(graph, _FindingEdgeContext(nodes_by_kind, refs, when_start, when_end))
    return graph


def _add_finding_nodes(graph: EntityGraph, finding: Finding, refs: Sequence[Any]) -> dict[str, list[str]]:
    nodes_by_kind: dict[str, list[str]] = {}
    for kind, values in finding.entities.items():
        _add_nodes_for_kind(graph, nodes_by_kind, _NodeBatch(kind, values, refs))
    return nodes_by_kind


def _add_nodes_for_kind(
    graph: EntityGraph,
    nodes_by_kind: dict[str, list[str]],
    batch: _NodeBatch,
) -> None:
    for value in batch.values:
        node_id = graph.add_node(batch.kind, value, batch.refs)
        if node_id:
            nodes_by_kind.setdefault(batch.kind, []).append(node_id)


def _add_finding_edges(graph: EntityGraph, context: _FindingEdgeContext) -> None:
    nodes_by_kind = context.nodes_by_kind
    window = {"first_seen": context.first_seen, "last_seen": context.last_seen}
    _add_edges(graph, _EdgeBatch(nodes_by_kind.get("attacker_ip", []), _first_present(nodes_by_kind, "victim_ip", "dst_ip", "host"), "targets", context.refs, **window))
    _add_edges(graph, _EdgeBatch(nodes_by_kind.get("user", []), _first_present(nodes_by_kind, "host", "victim_ip"), "authenticates_to", context.refs, **window))
    victims = _first_present(nodes_by_kind, "victim_ip", "host")
    _add_edges(graph, _EdgeBatch(victims, nodes_by_kind.get("process", []), "executes", context.refs, **window))
    _add_edges(graph, _EdgeBatch(victims, nodes_by_kind.get("dst_ip", []) + nodes_by_kind.get("domain", []), "connects_to", context.refs, **window))


def _add_edges(graph: EntityGraph, batch: _EdgeBatch) -> None:
    for source in batch.sources:
        for target in batch.targets:
            graph.add_edge(
                params=EntityEdgeInput(
                    source=source,
                    target=target,
                    relationship=batch.relationship,
                    evidence_refs=batch.refs,
                    first_seen=batch.first_seen,
                    last_seen=batch.last_seen,
                )
            )


def _first_present(values: dict[str, list[str]], *keys: str) -> list[str]:
    for key in keys:
        if values.get(key):
            return values[key]
    return []


def _extract_findings(value: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> list[Finding]:
    if isinstance(value, CaseRecord):
        attributes = value.attributes if isinstance(value.attributes, Mapping) else {}
        return [Finding.from_dict(item) for item in attributes.get("finding_summaries", []) if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        if "findings" in value:
            return [Finding.from_dict(item) for item in value.get("findings", [])]
        attributes = value.get("attributes")
        if isinstance(attributes, Mapping) and "finding_summaries" in attributes:
            return [Finding.from_dict(item) for item in attributes.get("finding_summaries", [])]
        return [Finding.from_dict(value)]
    return [item if isinstance(item, Finding) else Finding.from_dict(item) for item in value]


def _ref_ids(refs: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        result.append(_ref_id(ref))
    return _unique(result)


def _ref_id(ref: Any) -> str:
    if isinstance(ref, EvidenceRef):
        return ref.evidence_id
    if isinstance(ref, Mapping):
        return str(ref.get("evidence_id") or ref.get("raw_ref") or ref)
    return str(ref)


def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


__all__ = ["EntityEdge", "EntityGraph", "EntityNode", "build_entity_graph"]
