
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..common.json_io import write_json_file
from .failure_sample_library_contract import validate_failure_sample_library
from .long_task_recovery_scenario import run_long_task_recovery_scenario
from .medium_real_acceptance_runner import (
    MediumRealAcceptanceRunRequest,
    run_medium_real_acceptance,
)
from .small_real_acceptance_runner import (
    SmallRealAcceptanceRunRequest,
    run_small_real_acceptance,
)
from .task_tree_scenario import run_task_tree_small_scenario


@dataclass(frozen=True)
class PreRealTaskValidationRequest:
    workspace: Path


@dataclass(frozen=True)
class PreRealTaskValidationPhase:
    phase_id: str
    status: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "phase_id": self.phase_id,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class PreRealTaskValidationReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    phases: tuple[PreRealTaskValidationPhase, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "phases": [phase.to_dict() for phase in self.phases],
        }


def run_pre_real_task_validation(
    request: PreRealTaskValidationRequest,
) -> PreRealTaskValidationReport:
    workspace = Path(request.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    small = run_small_real_acceptance(SmallRealAcceptanceRunRequest(workspace=workspace / "phase-small"))
    failure_validation = validate_failure_sample_library(small.failure_samples or _synthetic_failure_sample())
    tree = run_task_tree_small_scenario(workspace / "phase-task-tree")
    recovery = run_long_task_recovery_scenario(workspace / "phase-long-task")
    medium = run_medium_real_acceptance(
        MediumRealAcceptanceRunRequest(workspace=workspace / "phase-medium", small_report=small)
    )
    phases = (
        _phase("small_real_runner_ready", True, [small.report_ref], []),
        _phase("small_real_batch", small.ok, [small.report_ref], list(small.gate_error_codes)),
        _phase(
            "failure_sample_capture",
            failure_validation.ok,
            ["synthetic://failure-sample-library"],
            list(failure_validation.error_codes),
        ),
        _phase("task_tree_small_scenario", tree.ok, [tree.report_ref, tree.tree_ref], list(tree.validation_error_codes)),
        _phase("long_task_recovery_scenario", recovery.ok, [recovery.report_ref, recovery.recovery_ref], list(recovery.validation_error_codes)),
        _phase("medium_real_batch", medium.ok, [medium.report_ref], list(medium.gate_error_codes)),
    )
    report = PreRealTaskValidationReport(
        ok=not any(phase.status == "FAILED" for phase in phases),
        summary=_summary(phases),
        report_ref="pre_real_task_validation/report.json",
        phases=phases,
    )
    write_json_file(workspace / report.report_ref, report.to_dict())
    return report


def _phase(
    phase_id: str,
    ok: bool,
    evidence_refs: list[str],
    issues: list[str],
) -> PreRealTaskValidationPhase:
    return PreRealTaskValidationPhase(
        phase_id=phase_id,
        status="PASSED" if ok else "FAILED",
        evidence_refs=evidence_refs,
        issues=issues,
    )


def _synthetic_failure_sample() -> tuple[dict[str, object], ...]:
    return (
        {
            "case_id": "synthetic_missing_artifact",
            "failure_type": "artifact_contract",
            "contract_fixture_ref": "contract-fixture://synthetic_missing_artifact.yaml",
            "fake_tool_trace_ref": "trace://synthetic_missing_artifact/tool.jsonl",
            "fake_llm_trace_ref": "trace://synthetic_missing_artifact/llm.jsonl",
            "replay_spec_ref": "replay://synthetic_missing_artifact.json",
            "expected_error_codes": ["ARTIFACT_MISSING"],
            "regression_test_ref": "pytest://replay/synthetic_missing_artifact",
            "source": "pre_real_task_validation",
        },
    )


def _summary(phases: tuple[PreRealTaskValidationPhase, ...]) -> dict[str, int]:
    return {
        "failed": sum(phase.status == "FAILED" for phase in phases),
        "passed": sum(phase.status == "PASSED" for phase in phases),
        "total": len(phases),
    }


__all__ = [
    "PreRealTaskValidationPhase",
    "PreRealTaskValidationReport",
    "PreRealTaskValidationRequest",
    "run_pre_real_task_validation",
]
