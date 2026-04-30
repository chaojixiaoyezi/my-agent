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
    NormalizedEvent,
    RawBatch,
    SecurityAlertV1,
    SourceSpec,
)


__all__ = [
    "CapabilityLevel",
    "Case",
    "CaseRecord",
    "Checkpoint",
    "EvidenceRef",
    "Finding",
    "LogAnalysisConfig",
    "NormalizedEvent",
    "RawBatch",
    "SecurityAlertV1",
    "SourceSpec",
    "collect_doctor_status",
    "describe_capability_levels",
    "describe_feature_gates",
    "effective_feature_gates",
    "is_feature_enabled",
    "load_log_analysis_config",
    "normalize_log_analysis_config",
]
