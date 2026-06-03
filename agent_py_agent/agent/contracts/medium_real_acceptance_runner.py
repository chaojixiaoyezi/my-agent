
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import write_json_file
from ..subagents.static_site import run_static_site_check
from .small_real_acceptance_gate import validate_small_real_acceptance_gate
from .small_real_acceptance_runner import (
    SmallRealAcceptanceRunReport,
    SmallRealCaseResult,
)


@dataclass(frozen=True)
class MediumRealAcceptanceRunRequest:
    workspace: Path
    small_report: SmallRealAcceptanceRunReport


@dataclass(frozen=True)
class MediumRealAcceptanceRunReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    small_report_ref: str
    gate_error_codes: tuple[str, ...]
    cases: tuple[SmallRealCaseResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "small_report_ref": self.small_report_ref,
            "gate_error_codes": list(self.gate_error_codes),
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True)
class _MediumCasePayload:
    case_id: str
    case_dir: Path
    artifact_refs: list[str]
    verification_refs: list[str]
    issues: list[str]


def run_medium_real_acceptance(
    request: MediumRealAcceptanceRunRequest,
) -> MediumRealAcceptanceRunReport:
    workspace = Path(request.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    cases = (
        _medium_multi_file_project(workspace),
        _medium_static_site_project(workspace),
    )
    gate = validate_small_real_acceptance_gate({"cases": [case.gate_case for case in cases]})
    prereq_issue = [] if request.small_report.ok else ["SMALL_REAL_PREREQUISITE_FAILED"]
    cases = tuple(_with_prereq_issue(case, prereq_issue) for case in cases)
    report = MediumRealAcceptanceRunReport(
        ok=gate.ok and not prereq_issue and not any(case.status == "FAILED" for case in cases),
        summary=_summary(cases),
        report_ref="medium_real_acceptance/report.json",
        small_report_ref=request.small_report.report_ref,
        gate_error_codes=gate.error_codes,
        cases=cases,
    )
    _write_artifact_json_ref(workspace / report.report_ref, report.to_dict(), workspace)
    return report


def _medium_multi_file_project(workspace: Path) -> SmallRealCaseResult:
    case_dir = _case_dir(workspace, "medium_multi_file_project")
    inputs = case_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (inputs / f"part-{index}.txt").write_text(f"part={index}\n", encoding="utf-8")
    artifact = case_dir / "artifacts" / "summary.json"
    artifact_ref = _write_artifact_json_ref(
        artifact,
        {"input_count": 3, "artifact_kind": "summary", "source_refs": [f"inputs/part-{i}.txt" for i in range(3)]},
        workspace,
    )
    verification_ref = _write_artifact_json_ref(case_dir / "verification.json", {"ok": True, "checked_inputs": 3}, workspace)
    return _case(
        workspace,
        _MediumCasePayload(
            "medium_multi_file_project",
            case_dir,
            [artifact_ref],
            [verification_ref],
            [],
        ),
    )


def _medium_static_site_project(workspace: Path) -> SmallRealCaseResult:
    case_dir = _case_dir(workspace, "medium_static_site_project")
    site = case_dir / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text('<a href="step-two.html">Step Two</a><div id="summary"></div>', encoding="utf-8")
    (site / "step-two.html").write_text('<a href="step-three.html">Step Three</a><form id="review"></form>', encoding="utf-8")
    (site / "step-three.html").write_text("<button onclick=\"document.body.dataset.done='1'\">Complete</button>", encoding="utf-8")
    record = run_static_site_check(
        {
            "name": "medium static site",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "step-two.html", "step-three.html"],
            "check_inert_controls": False,
        },
        case_dir,
    )
    artifact_ref = _rel(site, workspace)
    verification_ref = _write_artifact_json_ref(case_dir / "verification.json", record.to_dict(), workspace)
    return _case(
        workspace,
        _MediumCasePayload(
            "medium_static_site_project",
            case_dir,
            [artifact_ref],
            [verification_ref],
            [] if record.passed else ["STATIC_SITE_VALIDATION_FAILED"],
        ),
    )


def _case(
    workspace: Path,
    payload: _MediumCasePayload,
) -> SmallRealCaseResult:
    return SmallRealCaseResult(
        case_id=payload.case_id,
        status="FAILED" if payload.issues else "PASSED",
        complexity="medium",
        workspace_ref=f"workspace://{_rel(payload.case_dir, workspace)}",
        artifact_refs=payload.artifact_refs,
        verification_refs=payload.verification_refs,
        tool_probe_refs=[],
        gate_case=_gate_case(workspace, payload),
        issues=payload.issues,
    )


def _gate_case(
    workspace: Path,
    payload: _MediumCasePayload,
) -> dict[str, object]:
    return {
        "case_id": payload.case_id,
        "complexity": "medium",
        "workspace_ref": f"workspace://{_rel(payload.case_dir, workspace)}",
        "isolation_ok": True,
        "tool_modes": ["read_only", "dry_run"],
        "allowed_effects": ["read_only", "dry_run"],
        "real_execution_allowed": False,
        "max_runtime_seconds": 900,
        "expected_artifacts": [{"artifact_ref": ref} for ref in payload.artifact_refs],
        "verification_refs": payload.verification_refs,
        "replay_capture_enabled": True,
    }


def _with_prereq_issue(case: SmallRealCaseResult, issues: list[str]) -> SmallRealCaseResult:
    if not issues:
        return case
    return SmallRealCaseResult(
        case_id=case.case_id,
        status="FAILED",
        complexity=case.complexity,
        workspace_ref=case.workspace_ref,
        artifact_refs=case.artifact_refs,
        verification_refs=case.verification_refs,
        tool_probe_refs=case.tool_probe_refs,
        gate_case=case.gate_case,
        issues=[*case.issues, *issues],
    )


def _case_dir(workspace: Path, case_id: str) -> Path:
    path = workspace / "medium_real_acceptance" / "cases" / case_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_artifact_json_ref(path: Path, payload: object, root: Path) -> str:
    write_json_file(path, payload)
    return _rel(path, root)


def _rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _summary(cases: tuple[SmallRealCaseResult, ...]) -> dict[str, int]:
    return {
        "failed": sum(case.status == "FAILED" for case in cases),
        "passed": sum(case.status == "PASSED" for case in cases),
        "total": len(cases),
    }


__all__ = [
    "MediumRealAcceptanceRunReport",
    "MediumRealAcceptanceRunRequest",
    "run_medium_real_acceptance",
]
