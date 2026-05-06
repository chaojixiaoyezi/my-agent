from __future__ import annotations

"""Tests for agent_py_agent.agent.log_analysis.security.entity_graph."""

import pytest

from agent_py_agent.agent.log_analysis.models import CaseRecord, EvidenceRef, Finding
from agent_py_agent.agent.log_analysis.security.entity_graph import (
    EntityEdge,
    EntityGraph,
    EntityNode,
    build_entity_graph,
)

# ---------------------------------------------------------------------------
# EntityNode tests
# ---------------------------------------------------------------------------


class TestEntityNode:
    def test_node_id_format(self) -> None:
        node = EntityNode(kind="ip", value="10.0.0.1")
        assert node.node_id == "ip:10.0.0.1"

    def test_node_id_with_special_chars(self) -> None:
        node = EntityNode(kind="user", value="admin@example.com")
        assert node.node_id == "user:admin@example.com"

    def test_to_dict_includes_node_id(self) -> None:
        node = EntityNode(kind="host", value="web-01", evidence_refs=["ref-1"])
        result = node.to_dict()
        assert result["node_id"] == "host:web-01"
        assert result["kind"] == "host"
        assert result["value"] == "web-01"
        assert result["evidence_refs"] == ["ref-1"]

    def test_default_evidence_refs(self) -> None:
        node = EntityNode(kind="ip", value="1.2.3.4")
        assert node.evidence_refs == []


# ---------------------------------------------------------------------------
# EntityEdge tests
# ---------------------------------------------------------------------------


class TestEntityEdge:
    def test_to_dict(self) -> None:
        edge = EntityEdge(
            source="ip:1.1.1.1",
            target="ip:2.2.2.2",
            relationship="targets",
            evidence_refs=["e1"],
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-01T01:00:00Z",
        )
        result = edge.to_dict()
        assert result["source"] == "ip:1.1.1.1"
        assert result["target"] == "ip:2.2.2.2"
        assert result["relationship"] == "targets"
        assert result["evidence_refs"] == ["e1"]
        assert result["first_seen"] == "2026-01-01T00:00:00Z"
        assert result["last_seen"] == "2026-01-01T01:00:00Z"

    def test_default_values(self) -> None:
        edge = EntityEdge(source="a", target="b", relationship="rel")
        assert edge.evidence_refs == []
        assert edge.first_seen == ""
        assert edge.last_seen == ""


# ---------------------------------------------------------------------------
# EntityGraph tests
# ---------------------------------------------------------------------------


class TestEntityGraphAddNode:
    def test_add_new_node(self) -> None:
        graph = EntityGraph()
        node_id = graph.add_node("ip", "10.0.0.1")
        assert node_id == "ip:10.0.0.1"
        assert "ip:10.0.0.1" in graph.nodes

    def test_add_node_merges_evidence_refs(self) -> None:
        graph = EntityGraph()
        graph.add_node("ip", "10.0.0.1", evidence_refs=["ref-1"])
        graph.add_node("ip", "10.0.0.1", evidence_refs=["ref-2"])
        node = graph.nodes["ip:10.0.0.1"]
        assert "ref-1" in node.evidence_refs
        assert "ref-2" in node.evidence_refs

    def test_add_node_deduplicates_evidence_refs(self) -> None:
        graph = EntityGraph()
        graph.add_node("ip", "10.0.0.1", evidence_refs=["ref-1"])
        graph.add_node("ip", "10.0.0.1", evidence_refs=["ref-1"])
        node = graph.nodes["ip:10.0.0.1"]
        assert node.evidence_refs.count("ref-1") == 1

    def test_add_node_empty_kind_returns_empty(self) -> None:
        graph = EntityGraph()
        assert graph.add_node("", "value") == ""
        assert graph.add_node("  ", "value") == ""

    def test_add_node_empty_value_returns_empty(self) -> None:
        graph = EntityGraph()
        assert graph.add_node("ip", "") == ""
        assert graph.add_node("ip", "  ") == ""

    def test_add_node_none_values(self) -> None:
        graph = EntityGraph()
        assert graph.add_node(None, "value") == ""
        assert graph.add_node("ip", None) == ""

    def test_add_node_with_evidence_ref_objects(self) -> None:
        graph = EntityGraph()
        ref = EvidenceRef(evidence_id="ev-123")
        node_id = graph.add_node("host", "web-01", evidence_refs=[ref])
        assert node_id == "host:web-01"
        assert "ev-123" in graph.nodes["host:web-01"].evidence_refs

    def test_add_node_with_dict_evidence_refs(self) -> None:
        graph = EntityGraph()
        graph.add_node("ip", "1.2.3.4", evidence_refs=[{"evidence_id": "dict-ref"}])
        assert "dict-ref" in graph.nodes["ip:1.2.3.4"].evidence_refs


class TestEntityGraphAddEdge:
    def test_add_new_edge(self) -> None:
        graph = EntityGraph()
        graph.add_edge("ip:1.1.1.1", "ip:2.2.2.2", "targets")
        assert len(graph.edges) == 1
        assert graph.edges[0].source == "ip:1.1.1.1"
        assert graph.edges[0].relationship == "targets"

    def test_add_edge_merges_duplicate(self) -> None:
        graph = EntityGraph()
        graph.add_edge("a", "b", "rel", evidence_refs=["e1"])
        graph.add_edge("a", "b", "rel", evidence_refs=["e2"])
        assert len(graph.edges) == 1
        assert "e1" in graph.edges[0].evidence_refs
        assert "e2" in graph.edges[0].evidence_refs

    def test_add_edge_different_relationship_creates_new(self) -> None:
        graph = EntityGraph()
        graph.add_edge("a", "b", "targets")
        graph.add_edge("a", "b", "authenticates_to")
        assert len(graph.edges) == 2

    def test_add_edge_same_source_target_ignored(self) -> None:
        graph = EntityGraph()
        graph.add_edge("a", "a", "self")
        assert len(graph.edges) == 0

    def test_add_edge_empty_source_ignored(self) -> None:
        graph = EntityGraph()
        graph.add_edge("", "b", "rel")
        assert len(graph.edges) == 0

    def test_add_edge_empty_target_ignored(self) -> None:
        graph = EntityGraph()
        graph.add_edge("a", "", "rel")
        assert len(graph.edges) == 0

    def test_add_edge_merges_first_seen_keeps_earliest(self) -> None:
        graph = EntityGraph()
        graph.add_edge("a", "b", "rel", first_seen="2026-01-02T00:00:00Z")
        graph.add_edge("a", "b", "rel", first_seen="2026-01-01T00:00:00Z")
        # first_seen is kept from the first edge (not overwritten)
        assert graph.edges[0].first_seen == "2026-01-02T00:00:00Z"

    def test_add_edge_merges_last_seen_keeps_latest(self) -> None:
        graph = EntityGraph()
        graph.add_edge("a", "b", "rel", last_seen="2026-01-01T00:00:00Z")
        graph.add_edge("a", "b", "rel", last_seen="2026-01-02T00:00:00Z")
        # last_seen is overwritten with new value
        assert graph.edges[0].last_seen == "2026-01-02T00:00:00Z"


class TestEntityGraphToDict:
    def test_to_dict_empty_graph(self) -> None:
        graph = EntityGraph()
        result = graph.to_dict()
        assert result == {"nodes": [], "edges": []}

    def test_to_dict_with_nodes_and_edges(self) -> None:
        graph = EntityGraph()
        graph.add_node("ip", "1.1.1.1")
        graph.add_node("ip", "2.2.2.2")
        graph.add_edge("ip:1.1.1.1", "ip:2.2.2.2", "targets")
        result = graph.to_dict()
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1


# ---------------------------------------------------------------------------
# build_entity_graph tests
# ---------------------------------------------------------------------------


def _make_finding(**kwargs) -> Finding:
    finding_id = kwargs.pop("finding_id", "f-1")
    detector_id = kwargs.pop("detector_id", "test_detector")
    entities = kwargs.pop("entities", None)
    window = kwargs.pop("window", None)
    evidence_refs = kwargs.pop("evidence_refs", None)
    if kwargs:
        raise TypeError(f"Unexpected finding options: {sorted(kwargs)}")

    return Finding(
        finding_id=finding_id,
        detector_id=detector_id,
        entities=entities or {},
        window=window or [],
        evidence_refs=[EvidenceRef(evidence_id=r) for r in (evidence_refs or [])],
    )


class TestBuildEntityGraph:
    def test_empty_findings_list(self) -> None:
        graph = build_entity_graph([])
        assert len(graph.nodes) == 0
        assert len(graph.edges) == 0

    def test_single_finding_creates_nodes(self) -> None:
        finding = _make_finding(
            entities={"attacker_ip": ["1.1.1.1"], "victim_ip": ["10.0.0.1"]}
        )
        graph = build_entity_graph([finding])
        assert "attacker_ip:1.1.1.1" in graph.nodes
        assert "victim_ip:10.0.0.1" in graph.nodes

    def test_attacker_targets_victim_edge(self) -> None:
        finding = _make_finding(
            entities={"attacker_ip": ["1.1.1.1"], "victim_ip": ["10.0.0.1"]}
        )
        graph = build_entity_graph([finding])
        assert len(graph.edges) == 1
        edge = graph.edges[0]
        assert edge.source == "attacker_ip:1.1.1.1"
        assert edge.target == "victim_ip:10.0.0.1"
        assert edge.relationship == "targets"

    def test_user_authenticates_to_host(self) -> None:
        finding = _make_finding(
            entities={"user": ["admin"], "host": ["web-01"]}
        )
        graph = build_entity_graph([finding])
        assert len(graph.edges) == 1
        assert graph.edges[0].relationship == "authenticates_to"

    def test_victim_executes_process(self) -> None:
        finding = _make_finding(
            entities={"victim_ip": ["10.0.0.1"], "process": ["bash"]}
        )
        graph = build_entity_graph([finding])
        assert len(graph.edges) == 1
        assert graph.edges[0].relationship == "executes"

    def test_victim_connects_to_domain(self) -> None:
        finding = _make_finding(
            entities={"victim_ip": ["10.0.0.1"], "domain": ["evil.com"]}
        )
        graph = build_entity_graph([finding])
        assert len(graph.edges) == 1
        assert graph.edges[0].relationship == "connects_to"

    def test_victim_connects_to_dst_ip(self) -> None:
        finding = _make_finding(
            entities={"victim_ip": ["10.0.0.1"], "dst_ip": ["203.0.113.1"]}
        )
        graph = build_entity_graph([finding])
        # attacker_ip is absent, so no "targets" edge; victim_ip -> dst_ip = connects_to
        edge_rels = {e.relationship for e in graph.edges}
        assert "connects_to" in edge_rels

    def test_attacker_targets_fallback_to_dst_ip(self) -> None:
        finding = _make_finding(
            entities={"attacker_ip": ["1.1.1.1"], "dst_ip": ["10.0.0.1"]}
        )
        graph = build_entity_graph([finding])
        assert any(e.relationship == "targets" for e in graph.edges)

    def test_multiple_findings_merge_nodes(self) -> None:
        f1 = _make_finding(finding_id="f1", entities={"attacker_ip": ["1.1.1.1"], "victim_ip": ["10.0.0.1"]})
        f2 = _make_finding(finding_id="f2", entities={"attacker_ip": ["1.1.1.1"], "host": ["web-01"]})
        graph = build_entity_graph([f1, f2])
        # attacker_ip node appears in both findings, should be merged
        assert "attacker_ip:1.1.1.1" in graph.nodes

    def test_build_from_case_record(self) -> None:
        case = CaseRecord(
            case_id="c-1",
            title="test case",
            attributes={
                "finding_summaries": [
                    {
                        "finding_id": "f1",
                        "detector_id": "test",
                        "entities": {"attacker_ip": ["1.1.1.1"], "victim_ip": ["10.0.0.1"]},
                    }
                ]
            },
        )
        graph = build_entity_graph(case)
        assert "attacker_ip:1.1.1.1" in graph.nodes

    def test_build_from_mapping_with_findings_key(self) -> None:
        data = {
            "findings": [
                {
                    "finding_id": "f1",
                    "detector_id": "test",
                    "entities": {"host": ["web-01"], "user": ["admin"]},
                }
            ]
        }
        graph = build_entity_graph(data)
        assert "host:web-01" in graph.nodes
        assert "user:admin" in graph.nodes

    def test_build_from_mapping_without_findings_key(self) -> None:
        data = {
            "finding_id": "f1",
            "detector_id": "test",
            "entities": {"host": ["web-01"]},
        }
        graph = build_entity_graph(data)
        assert "host:web-01" in graph.nodes

    def test_evidence_refs_propagated_to_nodes(self) -> None:
        finding = _make_finding(
            entities={"host": ["web-01"]},
            evidence_refs=["ev-1", "ev-2"],
        )
        graph = build_entity_graph([finding])
        assert "ev-1" in graph.nodes["host:web-01"].evidence_refs

    def test_window_used_for_edge_timestamps(self) -> None:
        finding = _make_finding(
            entities={"attacker_ip": ["1.1.1.1"], "victim_ip": ["10.0.0.1"]},
            window=["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"],
        )
        graph = build_entity_graph([finding])
        assert graph.edges[0].first_seen == "2026-01-01T00:00:00Z"
        assert graph.edges[0].last_seen == "2026-01-01T01:00:00Z"
