"""测试实体图构建、关系推理和路径查找功能。

本模块测试日志分析中的实体图模块，包括：
1. EntityNode 和 EntityEdge 数据类
2. EntityGraph 节点和边的添加、去重、序列化
3. build_entity_graph 从 case/findings 构建完整图
4. 边界场景：孤立节点、循环关联、空输入
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.log_analysis.models import CaseRecord, EvidenceRef, Finding
from agent_py_agent.agent.log_analysis.security.entity_graph import (
    EntityEdge,
    EntityGraph,
    EntityNode,
    build_entity_graph,
)


def _finding(params):
    evidence = params["evidence"]
    return Finding(
        finding_id=params["finding_id"],
        detector_id=params["detector_id"],
        window=params["window"],
        entities=params["entities"],
        evidence_refs=[
            EvidenceRef(evidence_id=evidence[0], source_id=evidence[1], kind="query"),
        ],
    )


def _edges_between(graph, source, target):
    return [
        edge for edge in graph.edges
        if edge.source == source and edge.target == target
    ]


def _assert_nodes_present(graph, node_ids):
    for node_id in node_ids:
        assert node_id in graph.nodes


def _assert_edge_relationship(graph, source, target, relationship):
    edges = _edges_between(graph, source, target)
    assert len(edges) == 1
    assert edges[0].relationship == relationship


def _sample_entity_graph_findings():
    return [
        _finding(
            {
                "finding_id": "finding-1",
                "detector_id": "waf_attack_success_candidate",
                "window": ["2026-04-30T10:00:00Z", "2026-04-30T10:05:00Z"],
                "entities": {
                    "attacker_ip": ["198.51.100.1"],
                    "victim_ip": ["10.0.0.5"],
                    "user": ["admin"],
                },
                "evidence": ("ev-1", "waf-prod"),
            },
        ),
        _finding(
            {
                "finding_id": "finding-2",
                "detector_id": "web_to_process_anomaly",
                "window": ["2026-04-30T10:05:00Z", "2026-04-30T10:10:00Z"],
                "entities": {"victim_ip": ["10.0.0.5"], "process": ["/bin/bash"]},
                "evidence": ("ev-2", "edr-prod"),
            },
        ),
    ]


def test_entity_node_generates_unique_node_id():
    """测试 EntityNode 生成唯一的 node_id。

    node_id 应该是 kind:value 格式，用于唯一标识实体。
    """
    node = EntityNode(kind="attacker_ip", value="198.51.100.1")
    assert node.node_id == "attacker_ip:198.51.100.1"

    node2 = EntityNode(kind="user", value="admin")
    assert node2.node_id == "user:admin"


def test_entity_edge_serializes_to_dict():
    """测试 EntityEdge 能正确序列化为字典。"""
    edge = EntityEdge(
        source="attacker_ip:198.51.100.1",
        target="victim_ip:10.0.0.5",
        relationship="targets",
        evidence_refs=["ev-1", "ev-2"],
        first_seen="2026-04-30T10:00:00Z",
        last_seen="2026-04-30T10:05:00Z",
    )

    edge_dict = edge.to_dict()
    assert edge_dict["source"] == "attacker_ip:198.51.100.1"
    assert edge_dict["target"] == "victim_ip:10.0.0.5"
    assert edge_dict["relationship"] == "targets"
    assert edge_dict["evidence_refs"] == ["ev-1", "ev-2"]
    assert edge_dict["first_seen"] == "2026-04-30T10:00:00Z"
    assert edge_dict["last_seen"] == "2026-04-30T10:05:00Z"


def test_entity_graph_add_node_creates_and_dedupes():
    """测试 EntityGraph 添加节点并自动去重。"""
    graph = EntityGraph()

    node_id1 = graph.add_node("attacker_ip", "198.51.100.1", ["ev-1"])
    assert node_id1 == "attacker_ip:198.51.100.1"
    assert len(graph.nodes) == 1

    # 再次添加相同节点，应该更新 evidence_refs 而不是创建新节点
    node_id2 = graph.add_node("attacker_ip", "198.51.100.1", ["ev-2"])
    assert node_id2 == node_id1
    assert len(graph.nodes) == 1
    assert graph.nodes[node_id1].evidence_refs == ["ev-1", "ev-2"]


def test_entity_graph_add_node_handles_empty_input():
    """测试 EntityGraph 添加空值节点时返回空字符串。"""
    graph = EntityGraph()

    assert graph.add_node("", "198.51.100.1") == ""
    assert graph.add_node("attacker_ip", "") == ""
    assert graph.add_node("  ", "  ") == ""
    assert len(graph.nodes) == 0


def test_entity_graph_add_edge_creates_and_merges():
    """测试 EntityGraph 添加边并自动合并相同边。"""
    graph = EntityGraph()

    graph.add_edge(
        "attacker_ip:198.51.100.1",
        "victim_ip:10.0.0.5",
        "targets",
        ["ev-1"],
        first_seen="2026-04-30T10:00:00Z",
        last_seen="2026-04-30T10:02:00Z",
    )
    assert len(graph.edges) == 1
    assert graph.edges[0].evidence_refs == ["ev-1"]
    assert graph.edges[0].first_seen == "2026-04-30T10:00:00Z"

    # 添加相同关系，应该合并 evidence_refs 并更新时间
    graph.add_edge(
        "attacker_ip:198.51.100.1",
        "victim_ip:10.0.0.5",
        "targets",
        ["ev-2"],
        first_seen="",
        last_seen="2026-04-30T10:05:00Z",
    )
    assert len(graph.edges) == 1
    assert set(graph.edges[0].evidence_refs) == {"ev-1", "ev-2"}
    assert graph.edges[0].first_seen == "2026-04-30T10:00:00Z"  # 保持最早时间
    assert graph.edges[0].last_seen == "2026-04-30T10:05:00Z"


def test_entity_graph_add_edge_handles_self_loop():
    """测试 EntityGraph 忽略自循环边。"""
    graph = EntityGraph()

    graph.add_edge(
        "attacker_ip:198.51.100.1",
        "attacker_ip:198.51.100.1",
        "targets",
        ["ev-1"],
    )
    assert len(graph.edges) == 0


def test_entity_graph_add_edge_handles_empty_input():
    """测试 EntityGraph 处理空值边输入。"""
    graph = EntityGraph()

    graph.add_edge("", "victim_ip:10.0.0.5", "targets")
    graph.add_edge("attacker_ip:198.51.100.1", "", "targets")
    assert len(graph.edges) == 0


def test_entity_graph_to_dict_serializes_correctly():
    """测试 EntityGraph 正确序列化为字典。"""
    graph = EntityGraph()

    graph.add_node("attacker_ip", "198.51.100.1", ["ev-1"])
    graph.add_node("victim_ip", "10.0.0.5", ["ev-1"])
    graph.add_edge("attacker_ip:198.51.100.1", "victim_ip:10.0.0.5", "targets", ["ev-1"])

    result = graph.to_dict()
    assert "nodes" in result
    assert "edges" in result
    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 1
    assert result["nodes"][0]["node_id"] == "attacker_ip:198.51.100.1"


def test_build_entity_graph_from_findings():
    """测试从 findings 列表构建完整实体图。"""
    graph = build_entity_graph(_sample_entity_graph_findings())

    _assert_nodes_present(
        graph,
        [
            "attacker_ip:198.51.100.1",
            "victim_ip:10.0.0.5",
            "user:admin",
            "process:/bin/bash",
        ],
    )
    _assert_edge_relationship(
        graph,
        "attacker_ip:198.51.100.1",
        "victim_ip:10.0.0.5",
        "targets",
    )
    _assert_edge_relationship(
        graph,
        "user:admin",
        "victim_ip:10.0.0.5",
        "authenticates_to",
    )
    _assert_edge_relationship(
        graph,
        "victim_ip:10.0.0.5",
        "process:/bin/bash",
        "executes",
    )


def test_build_entity_graph_from_case_record():
    """测试从 CaseRecord 构建实体图。"""
    case = CaseRecord(
        case_id="case-1",
        title="web attack",
        attributes={
            "finding_summaries": [
                {
                    "finding_id": "finding-1",
                    "detector_id": "waf_attack_success_candidate",
                    "window": ["2026-04-30T10:00:00Z", "2026-04-30T10:05:00Z"],
                    "entities": {
                        "attacker_ip": ["198.51.100.1"],
                        "victim_ip": ["10.0.0.5"],
                    },
                    "evidence_refs": [{"evidence_id": "ev-1", "source_id": "waf-prod"}],
                }
            ]
        },
    )

    graph = build_entity_graph(case)

    assert "attacker_ip:198.51.100.1" in graph.nodes
    assert "victim_ip:10.0.0.5" in graph.nodes
    assert len(graph.edges) == 1
    assert graph.edges[0].relationship == "targets"


def test_build_entity_graph_handles_dst_ip_as_victim():
    """测试 build_entity_graph 使用 dst_ip 作为 victim_ip 的替代。"""
    findings = [
        Finding(
            finding_id="finding-1",
            detector_id="network_scan",
            window=["2026-04-30T10:00:00Z", "2026-04-30T10:05:00Z"],
            entities={
                "attacker_ip": ["198.51.100.1"],
                "dst_ip": ["10.0.0.5"],  # 使用 dst_ip 而不是 victim_ip
            },
        ),
    ]

    graph = build_entity_graph(findings)

    # 应该创建 attacker_ip 到 dst_ip 的边
    attacker_to_dst = [
        edge for edge in graph.edges
        if edge.source == "attacker_ip:198.51.100.1" and edge.target == "dst_ip:10.0.0.5"
    ]
    assert len(attacker_to_dst) == 1


def test_build_entity_graph_handles_domain_connections():
    """测试 build_entity_graph 创建到域名的连接。"""
    findings = [
        Finding(
            finding_id="finding-1",
            detector_id="c2_communication",
            window=["2026-04-30T10:00:00Z", "2026-04-30T10:05:00Z"],
            entities={
                "victim_ip": ["10.0.0.5"],
                "domain": ["evil.example.com"],
            },
        ),
    ]

    graph = build_entity_graph(findings)

    # 应该创建 victim_ip 到 domain 的边
    victim_to_domain = [
        edge for edge in graph.edges
        if edge.source == "victim_ip:10.0.0.5" and edge.target == "domain:evil.example.com"
    ]
    assert len(victim_to_domain) == 1
    assert victim_to_domain[0].relationship == "connects_to"


def test_build_entity_graph_handles_empty_findings():
    """测试 build_entity_graph 处理空 findings 列表。"""
    graph = build_entity_graph([])
    assert len(graph.nodes) == 0
    assert len(graph.edges) == 0


def test_build_entity_graph_handles_missing_window():
    """测试 build_entity_graph 处理缺少时间窗口的 findings。"""
    findings = [
        Finding(
            finding_id="finding-1",
            detector_id="test_detector",
            window=[],  # 空窗口
            entities={
                "attacker_ip": ["198.51.100.1"],
                "victim_ip": ["10.0.0.5"],
            },
        ),
    ]

    graph = build_entity_graph(findings)

    # 仍然应该创建节点和边，只是时间戳为空
    assert len(graph.nodes) == 2
    assert len(graph.edges) == 1
    assert graph.edges[0].first_seen == ""
    assert graph.edges[0].last_seen == ""


def test_build_entity_graph_merges_multiple_entities_of_same_kind():
    """测试多个相同类型的实体都能正确添加。"""
    findings = [
        Finding(
            finding_id="finding-1",
            detector_id="multi_target_attack",
            window=["2026-04-30T10:00:00Z", "2026-04-30T10:05:00Z"],
            entities={
                "attacker_ip": ["198.51.100.1"],
                "victim_ip": ["10.0.0.5", "10.0.0.6", "10.0.0.7"],
            },
        ),
    ]

    graph = build_entity_graph(findings)

    # 应该有 1 个攻击者和 3 个受害者
    assert "attacker_ip:198.51.100.1" in graph.nodes
    assert "victim_ip:10.0.0.5" in graph.nodes
    assert "victim_ip:10.0.0.6" in graph.nodes
    assert "victim_ip:10.0.0.7" in graph.nodes

    # 攻击者应该有 3 条边到不同的受害者
    attacker_edges = [
        edge for edge in graph.edges
        if edge.source == "attacker_ip:198.51.100.1"
    ]
    assert len(attacker_edges) == 3
