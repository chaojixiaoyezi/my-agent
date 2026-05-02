from __future__ import annotations

"""Optional log analysis contracts and safe defaults."""

from .capabilities import (
    CapabilityLevel,
    describe_capability_levels,
    describe_feature_gates,
    effective_feature_gates,
    is_feature_enabled,
)
from .config import LogAnalysisConfig, load_log_analysis_config, normalize_log_analysis_config
from .doctor import collect_doctor_status
from .models import (
    Case,
    CaseRecord,
    Checkpoint,
    EvidenceRef,
    Finding,
    JsonRoundTripMixin,
    LogWorkOrder,
    NormalizedEvent,
    QueryResult,
    RawBatch,
    SECURITY_ALERT_V1_FIELD_ALIASES,
    SecurityAlertV1,
    SecurityCase,
    SourceSpec,
    utc_now_iso,
)
from .work_order import work_order_to_subagent_task
from .bounded_query import bounded_query


__all__ = [
    "CapabilityLevel",
    "Case",
    "CaseRecord",
    "Checkpoint",
    "EvidenceRef",
    "Finding",
    "JsonRoundTripMixin",
    "LogAnalysisConfig",
    "LogWorkOrder",
    "NormalizedEvent",
    "QueryResult",
    "RawBatch",
    "SECURITY_ALERT_V1_FIELD_ALIASES",
    "SecurityAlertV1",
    "SecurityCase",
    "SourceSpec",
    "bounded_query",
    "collect_doctor_status",
    "describe_capability_levels",
    "describe_feature_gates",
    "effective_feature_gates",
    "is_feature_enabled",
    "load_log_analysis_config",
    "normalize_log_analysis_config",
    "utc_now_iso",
    "work_order_to_subagent_task",
]
