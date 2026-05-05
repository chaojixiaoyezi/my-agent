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
        source: str,
        target: str,
        relationship: str,
        evidence_refs: Sequence[Any] = (),
        *,
        first_seen: str = "",
        last_seen: str = "",
    ) -> None:
        if not source or not target or source == target:
            return
        refs = _ref_ids(evidence_refs)
        for edge in self.edges:
            if edge.source == source and edge.target == target and edge.relationship == relationship:
                edge.evidence_refs = _unique([*edge.evidence_refs, *refs])
                edge.first_seen = edge.first_seen or first_seen
                edge.last_seen = last_seen or edge.last_seen
                return
        self.edges.append(
            EntityEdge(
                source=source,
                target=target,
                relationship=relationship,
                evidence_refs=refs,
                first_seen=first_seen,
                last_seen=last_seen,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
        }


def build_entity_graph(case_or_findings: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> EntityGraph:
    graph = EntityGraph()
    for finding in _extract_findings(case_or_findings):
        refs = finding.evidence_refs
        when_start = finding.window[0] if finding.window else ""
        when_end = finding.window[1] if len(finding.window) > 1 else when_start
        nodes_by_kind = _add_finding_nodes(graph, finding, refs)
        _add_finding_edges(graph, nodes_by_kind, refs, first_seen=when_start, last_seen=when_end)
    return graph


def _add_finding_nodes(graph: EntityGraph, finding: Finding, refs: Sequence[Any]) -> dict[str, list[str]]:
    nodes_by_kind: dict[str, list[str]] = {}
    for kind, values in finding.entities.items():
        for value in values:
            node_id = graph.add_node(kind, value, refs)
            if node_id:
                nodes_by_kind.setdefault(kind, []).append(node_id)
    return nodes_by_kind


def _add_finding_edges(
    graph: EntityGraph,
    nodes_by_kind: dict[str, list[str]],
    refs: Sequence[Any],
    *,
    first_seen: str,
    last_seen: str,
) -> None:
    window = {"first_seen": first_seen, "last_seen": last_seen}
    _add_edges(graph, nodes_by_kind.get("attacker_ip", []), _first_present(nodes_by_kind, "victim_ip", "dst_ip", "host"), "targets", refs, **window)
    _add_edges(graph, nodes_by_kind.get("user", []), _first_present(nodes_by_kind, "host", "victim_ip"), "authenticates_to", refs, **window)
    victims = _first_present(nodes_by_kind, "victim_ip", "host")
    _add_edges(graph, victims, nodes_by_kind.get("process", []), "executes", refs, **window)
    _add_edges(graph, victims, nodes_by_kind.get("dst_ip", []) + nodes_by_kind.get("domain", []), "connects_to", refs, **window)


def _add_edges(graph: EntityGraph, sources: Sequence[str], targets: Sequence[str], relationship: str, refs: Sequence[Any], **window: str) -> None:
    for source in sources:
        for target in targets:
            graph.add_edge(source, target, relationship, refs, first_seen=window.get("first_seen", ""), last_seen=window.get("last_seen", ""))


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
        if isinstance(ref, EvidenceRef):
            result.append(ref.evidence_id)
        elif isinstance(ref, Mapping):
            result.append(str(ref.get("evidence_id") or ref.get("raw_ref") or ref))
        else:
            result.append(str(ref))
    return _unique(result)


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
