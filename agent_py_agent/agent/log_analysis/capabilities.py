
from __future__ import annotations

"""Capability levels and feature gates for log analysis."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .config import LogAnalysisConfig

SECURITY_TOOL_NAMES = ("security_query", "security_hunt_ip", "security_trace_case")
SECURITY_TOOL_CAPABILITIES = (
    "log_analysis",
    "logs",
    "security",
    "security_logs",
    "logs/security",
)


class CapabilityLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"


@dataclass(frozen=True)
class LevelInfo:
    level: CapabilityLevel
    title: str
    description: str


@dataclass(frozen=True)
class FeatureGate:
    feature: str
    min_level: CapabilityLevel
    description: str
    config_flag: str = ""
    allow_when_disabled: bool = False


LEVELS: dict[CapabilityLevel, LevelInfo] = {
    CapabilityLevel.L0: LevelInfo(CapabilityLevel.L0, "disabled", "status and doctor only"),
    CapabilityLevel.L1: LevelInfo(CapabilityLevel.L1, "local", "local file ingest, normalization, storage, query, evidence, and cases"),
    CapabilityLevel.L2: LevelInfo(CapabilityLevel.L2, "analyst", "manual analyst workflows, route drafts, and security prompts"),
    CapabilityLevel.L3: LevelInfo(CapabilityLevel.L3, "automation", "continuous workers, scans, and explicitly budgeted dispatch"),
    CapabilityLevel.L4: LevelInfo(CapabilityLevel.L4, "external-backends", "cluster and external backend adapters"),
    CapabilityLevel.L5: LevelInfo(CapabilityLevel.L5, "response", "response connectors with explicit execution gates"),
}


FEATURE_GATES: dict[str, FeatureGate] = {
    "status": FeatureGate("status", CapabilityLevel.L0, "read-only module status", allow_when_disabled=True),
    "source_config": FeatureGate("source_config", CapabilityLevel.L1, "source specification management"),
    "file_ingest": FeatureGate("file_ingest", CapabilityLevel.L1, "local file ingestion"),
    "local_store": FeatureGate("local_store", CapabilityLevel.L1, "local event and metadata storage"),
    "query": FeatureGate("query", CapabilityLevel.L1, "bounded event queries"),
    "evidence_refs": FeatureGate("evidence_refs", CapabilityLevel.L1, "query/sample/raw evidence references"),
    "detectors": FeatureGate("detectors", CapabilityLevel.L1, "local deterministic detectors"),
    "case_management": FeatureGate("case_management", CapabilityLevel.L1, "finding to case lifecycle"),
    "security_prompt": FeatureGate("security_prompt", CapabilityLevel.L2, "security analysis prompt injection", "security_prompt_enabled"),
    "manual_dispatch": FeatureGate("manual_dispatch", CapabilityLevel.L2, "manual analyst/reviewer dispatch"),
    "response_recommendations": FeatureGate("response_recommendations", CapabilityLevel.L2, "recommend or dry-run response plans"),
    "continuous_workers": FeatureGate("continuous_workers", CapabilityLevel.L3, "background watch and scan workers", "worker_enabled"),
    "auto_dispatch": FeatureGate("auto_dispatch", CapabilityLevel.L3, "automatic analyst dispatch", "auto_dispatch_enabled"),
    "ml_scoring": FeatureGate("ml_scoring", CapabilityLevel.L3, "ML scoring engines", "ml_enabled"),
    "cluster_backend": FeatureGate("cluster_backend", CapabilityLevel.L4, "Kafka/ClickHouse/Flink/Ray/Spark adapters", "cluster_enabled"),
    "response_execution": FeatureGate("response_execution", CapabilityLevel.L5, "non-dry-run response connector execution", "response_execution_enabled"),
}


def normalize_level(level: str | CapabilityLevel) -> CapabilityLevel:
    if isinstance(level, CapabilityLevel):
        return level
    try:
        return CapabilityLevel(str(level).strip().upper())
    except ValueError:
        return CapabilityLevel.L0


def level_rank(level: str | CapabilityLevel) -> int:
    return list(CapabilityLevel).index(normalize_level(level))


def feature_gate_for(feature: str) -> FeatureGate:
    try:
        return FEATURE_GATES[feature]
    except KeyError as exc:
        raise KeyError(f"unknown log analysis feature gate: {feature}") from exc


def is_feature_enabled(config: LogAnalysisConfig | str | CapabilityLevel, feature: str) -> bool:
    gate = feature_gate_for(feature)

    if isinstance(config, LogAnalysisConfig):
        if gate.allow_when_disabled and feature == "status":
            return True
        if not config.enabled:
            return False
        if level_rank(config.capability_level) < level_rank(gate.min_level):
            return False
        if gate.config_flag and not bool(getattr(config, gate.config_flag, False)):
            return False
        if feature == "auto_dispatch":
            return config.max_parallel_analyst_agents > 0 and config.dispatch_budget_per_hour > 0
        if feature == "response_execution":
            return config.response_mode == "execute"
        return True

    return level_rank(config) >= level_rank(gate.min_level)


def effective_feature_gates(config: LogAnalysisConfig) -> dict[str, bool]:
    return {feature: is_feature_enabled(config, feature) for feature in FEATURE_GATES}


def describe_capability_levels() -> list[dict[str, str]]:
    return [
        {
            "level": info.level.value,
            "title": info.title,
            "description": info.description,
        }
        for info in LEVELS.values()
    ]


def describe_feature_gates(config: LogAnalysisConfig | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for gate in FEATURE_GATES.values():
        row: dict[str, Any] = {
            "feature": gate.feature,
            "min_level": gate.min_level.value,
            "config_flag": gate.config_flag,
            "description": gate.description,
        }
        if config is not None:
            row["enabled"] = is_feature_enabled(config, gate.feature)
        rows.append(row)
    return rows


def has_security_tool_capability(grants: list[str] | tuple[str, ...] | set[str] | None) -> bool:
    if not grants:
        return False
    normalized = {str(item).strip().lower() for item in grants if str(item).strip()}
    return bool(normalized.intersection(SECURITY_TOOL_CAPABILITIES))


def security_tool_names_for_capabilities(
    grants: list[str] | tuple[str, ...] | set[str] | None,
) -> list[str]:
    if has_security_tool_capability(grants):
        return list(SECURITY_TOOL_NAMES)
    return []


__all__ = [
    "CapabilityLevel",
    "FEATURE_GATES",
    "FeatureGate",
    "LEVELS",
    "LevelInfo",
    "SECURITY_TOOL_CAPABILITIES",
    "SECURITY_TOOL_NAMES",
    "describe_capability_levels",
    "describe_feature_gates",
    "effective_feature_gates",
    "feature_gate_for",
    "has_security_tool_capability",
    "is_feature_enabled",
    "level_rank",
    "normalize_level",
    "security_tool_names_for_capabilities",
]
