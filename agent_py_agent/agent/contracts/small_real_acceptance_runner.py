
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.json_io import write_json_file
from ..tooling.runtime_contracts import ToolCall, ToolResult
from .failure_sample_capture import failure_samples_from_case_results
from .real_tool_dry_run_contract import validate_real_tool_dry_run_probes
from .small_real_acceptance_gate import validate_small_real_acceptance_gate
from .small_real_acceptance_probes import (
    controlled_exec_grant,
    controlled_exec_probe,
    read_file_probe,
    small_real_registry,
)


@dataclass(frozen=True)
class SmallRealAcceptanceRunRequest:
    workspace: Path
    case_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SmallRealCaseResult:
    case_id: str
    status: str
    complexity: str
    workspace_ref: str
    artifact_refs: list[str] = field(default_factory=list)
    verification_refs: list[str] = field(default_factory=list)
    tool_probe_refs: list[str] = field(default_factory=list)
    gate_case: dict[str, object] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "complexity": self.complexity,
            "workspace_ref": self.workspace_ref,
            "artifact_refs": list(self.artifact_refs),
            "verification_refs": list(self.verification_refs),
            "tool_probe_refs": list(self.tool_probe_refs),
            "gate_case": dict(self.gate_case),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class SmallRealAcceptanceRunReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    gate_error_codes: tuple[str, ...]
    cases: tuple[SmallRealCaseResult, ...]
    failure_samples: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "gate_error_codes": list(self.gate_error_codes),
            "cases": [case.to_dict() for case in self.cases],
            "failure_samples": list(self.failure_samples),
        }


@dataclass(frozen=True)
class _CaseOutcome:
    case_id: str
    status: str
    case_dir: Path
    artifact_refs: list[str]
    verification_refs: list[str]
    tool_probe_refs: list[str]
    issues: list[str]
    complexity: str = "small"


def run_small_real_acceptance(
    request: SmallRealAcceptanceRunRequest,
) -> SmallRealAcceptanceRunReport:
    workspace = Path(request.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    selected = set(request.case_ids)
    run_context = _SmallRunContext(workspace)
    cases = [
        _run_real_read_file(run_context),
        _run_controlled_exec_dry_run(run_context),
    ]
    cases = tuple(case for case in cases if not selected or case.case_id in selected)
    gate = validate_small_real_acceptance_gate({"cases": [case.gate_case for case in cases]})
    samples = failure_samples_from_case_results(cases)
    report = SmallRealAcceptanceRunReport(
        ok=gate.ok and not any(case.status == "FAILED" for case in cases),
        summary=_summary(cases),
        report_ref="small_real_acceptance/report.json",
        gate_error_codes=gate.error_codes,
        cases=cases,
        failure_samples=samples,
    )
    write_json_file(workspace / report.report_ref, report.to_dict())
    return report


class _SmallRunContext:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.registry = small_real_registry(workspace)
        self.probe_refs: list[dict[str, object]] = []


def _run_real_read_file(context: _SmallRunContext) -> SmallRealCaseResult:
    case_dir = _case_dir(context.workspace, "real_read_file")
    input_path = case_dir / "input.txt"
    input_path.write_text("hello from real read_file wrapper\n", encoding="utf-8")
    result = _execute_probe_tool(
        context,
        "read_file",
        {"path": _rel(input_path, context.workspace)},
        allowed_tools=["read_file"],
    )
    probe = read_file_probe(result)
    return _tool_probe_case(context, "real_read_file", case_dir, probe)


def _run_controlled_exec_dry_run(context: _SmallRunContext) -> SmallRealCaseResult:
    case_dir = _case_dir(context.workspace, "controlled_exec_dry_run")
    result = _execute_probe_tool(
        context,
        "controlled_exec",
        {"command": "pwd", "apply": False, "cwd": _rel(case_dir, context.workspace)},
        allowed_tools=["controlled_exec"],
        write_boundary={"controlled_exec_grants": [controlled_exec_grant(case_dir)]},
    )
    probe = controlled_exec_probe(result)
    return _tool_probe_case(context, "controlled_exec_dry_run", case_dir, probe)


def _execute_probe_tool(
    context: _SmallRunContext,
    tool_name: str,
    arguments: dict[str, object],
    *,
    allowed_tools: list[str],
    write_boundary: dict[str, object] | None = None,
) -> ToolResult:
    run_id = f"small-real:{tool_name}"
    snapshot = context.registry.runtime_snapshot(
        allowed_tools=allowed_tools,
        run_id=run_id,
    )
    runtime = snapshot.runtime(tool_name)
    if runtime is None:
        raise RuntimeError(f"small real probe tool unavailable: {tool_name}")
    call = ToolCall(
        call_id=f"probe:{tool_name}",
        tool_name=tool_name,
        arguments=arguments,
        source_protocol="native",
        schema_hash=runtime.model_spec.schema_hash,
        run_id=run_id,
        turn_id=f"{run_id}:turn",
        attempt_id=f"{run_id}:attempt",
    )
    return context.registry.execute_tool(
        call,
        write_boundary=write_boundary,
        runtime_snapshot=snapshot,
    ).result


def _tool_probe_case(
    context: _SmallRunContext,
    case_id: str,
    case_dir: Path,
    probe: dict[str, object],
) -> SmallRealCaseResult:
    validation = validate_real_tool_dry_run_probes((probe,))
    probe_ref = _write_artifact_json_ref(case_dir / "probe.json", probe, root=context.workspace)
    verification_ref = _write_artifact_json_ref(case_dir / "verification.json", _validation_dict(validation), root=context.workspace)
    context.probe_refs.append(
        {
            "probe_id": probe["probe_id"],
            "contract_ref": f"artifact://{probe_ref}",
            "validation_ok": validation.ok,
            "effect": probe["effect"],
            "mode": probe["mode"],
            "tool_executor_ref": probe["tool_executor_ref"],
        }
    )
    return _case_result(
        context.workspace,
        _CaseOutcome(
            case_id=case_id,
            status="PASSED" if validation.ok else "FAILED",
            case_dir=case_dir,
            artifact_refs=[probe_ref],
            verification_refs=[verification_ref],
            tool_probe_refs=[probe_ref],
            issues=list(validation.error_codes),
        ),
    )


def _case_result(
    workspace: Path,
    outcome: _CaseOutcome,
) -> SmallRealCaseResult:
    return SmallRealCaseResult(
        case_id=outcome.case_id,
        status=outcome.status,
        complexity=outcome.complexity,
        workspace_ref=f"workspace://{_rel(outcome.case_dir, workspace)}",
        artifact_refs=outcome.artifact_refs,
        verification_refs=outcome.verification_refs,
        tool_probe_refs=outcome.tool_probe_refs,
        gate_case=_gate_case(workspace, outcome),
        issues=outcome.issues,
    )


def _gate_case(
    workspace: Path,
    outcome: _CaseOutcome,
) -> dict[str, object]:
    return {
        "case_id": outcome.case_id,
        "complexity": outcome.complexity,
        "workspace_ref": f"workspace://{_rel(outcome.case_dir, workspace)}",
        "isolation_ok": True,
        "tool_modes": ["read_only", "dry_run"],
        "allowed_effects": ["read_only", "dry_run"],
        "real_execution_allowed": False,
        "max_runtime_seconds": 300 if outcome.complexity == "small" else 900,
        "expected_artifacts": [{"artifact_ref": ref} for ref in outcome.artifact_refs],
        "verification_refs": outcome.verification_refs,
        "replay_capture_enabled": True,
    }


def _case_dir(workspace: Path, case_id: str) -> Path:
    path = workspace / "small_real_acceptance" / "cases" / case_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_artifact_json_ref(path: Path, payload: object, *, root: Path) -> str:
    write_json_file(path, payload)
    return _rel(path, root)


def _validation_dict(validation) -> dict[str, object]:
    return {
        "ok": validation.ok,
        "error_codes": list(validation.error_codes),
        "findings": list(validation.findings),
    }


def _rel(path: Path, root: Path | None) -> str:
    return str(path.resolve().relative_to(root.resolve())) if root else str(path)


def _summary(cases: tuple[SmallRealCaseResult, ...]) -> dict[str, int]:
    return {
        "failed": sum(case.status == "FAILED" for case in cases),
        "passed": sum(case.status == "PASSED" for case in cases),
        "total": len(cases),
    }


__all__ = [
    "SmallRealAcceptanceRunReport",
    "SmallRealAcceptanceRunRequest",
    "SmallRealCaseResult",
    "run_small_real_acceptance",
]
