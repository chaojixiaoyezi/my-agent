
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RealRunRecord:
    run_id: str
    run_ref: str
    task_types: tuple[str, ...]
    final_status: str
    first_failure_code: str
    last_surface_code: str
    failure_stage: str
    root_cause_tags: tuple[str, ...]
    priority: str
    evidence_refs: tuple[str, ...]
    has_offline_regression_test: bool
    recommended_offline_test: str
    missing_artifact: bool = False
    empty_artifact: bool = False
    artifact_path_mismatch: bool = False
    tool_failed: bool = False
    tool_param_error: bool = False
    tool_result_format_error: bool = False
    repeated_tool_call: bool = False
    contract_acceptance_failed: bool = False
    dry_run_real_conflict: bool = False
    approval_anomaly: bool = False
    invalid_state_transition: bool = False
    context_contract_lost: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "run_ref": self.run_ref,
            "task_types": list(self.task_types),
            "final_status": self.final_status,
            "first_failure_code": self.first_failure_code,
            "last_surface_code": self.last_surface_code,
            "failure_stage": self.failure_stage,
            "root_cause_tags": list(self.root_cause_tags),
            "priority": self.priority,
            "evidence_refs": list(self.evidence_refs),
            "has_offline_regression_test": self.has_offline_regression_test,
            "recommended_offline_test": self.recommended_offline_test,
            "missing_artifact": self.missing_artifact,
            "empty_artifact": self.empty_artifact,
            "artifact_path_mismatch": self.artifact_path_mismatch,
            "tool_failed": self.tool_failed,
            "tool_param_error": self.tool_param_error,
            "tool_result_format_error": self.tool_result_format_error,
            "repeated_tool_call": self.repeated_tool_call,
            "contract_acceptance_failed": self.contract_acceptance_failed,
            "dry_run_real_conflict": self.dry_run_real_conflict,
            "approval_anomaly": self.approval_anomaly,
            "invalid_state_transition": self.invalid_state_transition,
            "context_contract_lost": self.context_contract_lost,
        }


@dataclass(frozen=True)
class FailurePattern:
    tag: str
    count: int
    priority: str
    run_ids: tuple[str, ...]
    first_failure_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "tag": self.tag,
            "count": self.count,
            "priority": self.priority,
            "run_ids": list(self.run_ids),
            "first_failure_codes": list(self.first_failure_codes),
        }


@dataclass(frozen=True)
class RealRunReview:
    summary: dict[str, int]
    records: tuple[RealRunRecord, ...]
    clusters: tuple[FailurePattern, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": dict(self.summary),
            "records": [record.to_dict() for record in self.records],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }


__all__ = ["FailurePattern", "RealRunRecord", "RealRunReview"]
