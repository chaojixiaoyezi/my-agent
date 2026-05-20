# LLM: Medium real acceptance runner runs bounded multi-artifact cases after the small gate.
# 模块用途: 在大型真实任务前跑中型、隔离、无副作用的多文件验收样例。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..subagents.static_site_validator import run_static_site_check
from .small_real_acceptance_gate import validate_small_real_acceptance_gate
from .small_real_acceptance_runner import (
    SmallRealAcceptanceRunReport,
    SmallRealCaseResult,
)


# LLM: MediumRealAcceptanceRunRequest keeps the medium gate tied to a small report.
# 类用途: 描述中型验收工作区和必须先通过的小真实报告。
@dataclass(frozen=True)
class MediumRealAcceptanceRunRequest:
    workspace: Path
    small_report: SmallRealAcceptanceRunReport


# LLM: MediumRealAcceptanceRunReport summarizes bounded medium cases.
# 类用途: 保存中型验收报告、前置小真实报告 ref、闸门错误码和 case 列表。
@dataclass(frozen=True)
class MediumRealAcceptanceRunReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    small_report_ref: str
    gate_error_codes: tuple[str, ...]
    cases: tuple[SmallRealCaseResult, ...]

    # LLM: to_dict serializes medium case results without artifact bodies.
    # 函数用途: 输出中型验收批次报告，保持 refs-first。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "small_report_ref": self.small_report_ref,
            "gate_error_codes": list(self.gate_error_codes),
            "cases": [case.to_dict() for case in self.cases],
        }


# LLM: _MediumCasePayload carries intermediate medium case facts.
# 类用途: 打包 case id、路径、refs 和 issues，避免 helper 参数变宽。
@dataclass(frozen=True)
class _MediumCasePayload:
    case_id: str
    case_dir: Path
    artifact_refs: list[str]
    verification_refs: list[str]
    issues: list[str]


# LLM: run_medium_real_acceptance requires a passing small report before multi-artifact checks.
# 函数用途: 执行中型文件项目和静态站点项目验收，仍然只生成 refs 和 dry-run/read-only 事实。
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
    _write_json(workspace / report.report_ref, report.to_dict(), workspace)
    return report


# LLM: _medium_multi_file_project creates a deterministic multi-file artifact case.
# 函数用途: 生成多输入、多 artifact ref 的中型文件项目验收样例。
def _medium_multi_file_project(workspace: Path) -> SmallRealCaseResult:
    case_dir = _case_dir(workspace, "medium_multi_file_project")
    inputs = case_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (inputs / f"part-{index}.txt").write_text(f"part={index}\n", encoding="utf-8")
    artifact = case_dir / "artifacts" / "summary.json"
    artifact_ref = _write_json(
        artifact,
        {"input_count": 3, "artifact_kind": "summary", "source_refs": [f"inputs/part-{i}.txt" for i in range(3)]},
        workspace,
    )
    verification_ref = _write_json(case_dir / "verification.json", {"ok": True, "checked_inputs": 3}, workspace)
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


# LLM: _medium_static_site_project creates and validates a bounded static web artifact.
# 函数用途: 生成三页静态站点并用通用 static_site_check 验收。
def _medium_static_site_project(workspace: Path) -> SmallRealCaseResult:
    case_dir = _case_dir(workspace, "medium_static_site_project")
    site = case_dir / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text('<a href="cart.html">Cart</a><div id="cart"></div>', encoding="utf-8")
    (site / "cart.html").write_text('<a href="checkout.html">Checkout</a><form id="checkout"></form>', encoding="utf-8")
    (site / "checkout.html").write_text("<button onclick=\"document.body.dataset.done='1'\">Pay</button>", encoding="utf-8")
    record = run_static_site_check(
        {
            "name": "medium static site",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "cart.html", "checkout.html"],
            "check_inert_controls": False,
        },
        case_dir,
    )
    artifact_ref = _rel(site, workspace)
    verification_ref = _write_json(case_dir / "verification.json", record.to_dict(), workspace)
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


# LLM: _case converts medium payload facts into the shared case result shape.
# 函数用途: 统一生成中型 SmallRealCaseResult，复用小真实闸门字段。
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


# LLM: _gate_case builds the medium gate payload from explicit refs.
# 函数用途: 固定 complexity=medium、只读/dry-run 边界和 artifact 验收 refs。
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


# LLM: _with_prereq_issue fails medium cases when the small gate did not pass.
# 函数用途: 将小真实前置失败作为结构化 issue 传播到中型 case。
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


# LLM: _case_dir allocates one deterministic medium case workspace.
# 函数用途: 创建中型 case 隔离目录，避免产物互相覆盖。
def _case_dir(workspace: Path, case_id: str) -> Path:
    path = workspace / "medium_real_acceptance" / "cases" / case_id
    path.mkdir(parents=True, exist_ok=True)
    return path


# LLM: _write_json persists medium evidence and returns a relative ref.
# 函数用途: 写入中型验收 artifact/verification/report JSON。
def _write_json(path: Path, payload: object, root: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return _rel(path, root)


# LLM: _rel converts paths to workspace-relative refs.
# 函数用途: 生成报告中使用的稳定相对路径。
def _rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


# LLM: _summary counts medium case statuses.
# 函数用途: 生成 passed/failed/total 摘要，只读结构化 status 字段。
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
