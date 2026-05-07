# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Public API for log-analysis security correlation."""

from .attack_chain import AttackChainStep, build_attack_chain, lateral_movement_signs
from .correlation import RouteDraft, build_route_draft, draft_route, route_from_case
from .entity_graph import EntityEdge, EntityGraph, EntityNode, build_entity_graph
from .hunting import (
    HuntQuery,
    build_case_hunt_plan,
    build_seed_hunt_queries,
    next_query_plan,
    retrohunt_query_plan,
)

__all__ = [
    "AttackChainStep",
    "EntityEdge",
    "EntityGraph",
    "EntityNode",
    "HuntQuery",
    "RouteDraft",
    "build_attack_chain",
    "build_case_hunt_plan",
    "build_entity_graph",
    "build_route_draft",
    "build_seed_hunt_queries",
    "draft_route",
    "lateral_movement_signs",
    "next_query_plan",
    "retrohunt_query_plan",
    "route_from_case",
]
