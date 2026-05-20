# LLM: Pre-real task validation orchestrates the six bounded gates before large real tasks.
# 模块用途: 将小真实、失败样本、任务树、长任务恢复和中型验收串成一个 refs-first 报告。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

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


# LLM: PreRealTaskValidationRequest keeps the six-phase runner input explicit.
# 类用途: 描述预真实任务验收的输出工作区。
@dataclass(frozen=True)
class PreRealTaskValidationRequest:
    workspace: Path


# LLM: PreRealTaskValidationPhase records one phase result.
# 类用途: 保存阶段 id、状态、证据引用和结构化问题。
@dataclass(frozen=True)
class PreRealTaskValidationPhase:
    phase_id: str
    status: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    # LLM: to_dict serializes one phase result.
    # 函数用途: 输出阶段报告，保持 evidence refs 和 issues 可机器读取。
    def to_dict(self) -> dict[str, object]:
        return {
            "phase_id": self.phase_id,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


# LLM: PreRealTaskValidationReport summarizes the full phase-1-to-6 run.
# 类用途: 保存预真实任务总报告、摘要和阶段结果列表。
@dataclass(frozen=True)
class PreRealTaskValidationReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    phases: tuple[PreRealTaskValidationPhase, ...]

    # LLM: to_dict keeps the orchestration report bounded and refs-first.
    # 函数用途: 将总报告转成 JSON 友好的结构，不内联任何 artifact 正文。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "phases": [phase.to_dict() for phase in self.phases],
        }


# LLM: run_pre_real_task_validation is the public phase-1-to-6 entrypoint.
# 函数用途: 顺序执行六个预真实任务阶段，把每步状态和 evidence refs 写入总报告。
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
    _write_json(workspace / report.report_ref, report.to_dict())
    return report


# LLM: _phase maps a boolean validation result to a stable phase status.
# 函数用途: 统一生成阶段状态对象，不依赖自然语言摘要判断成败。
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


# LLM: _synthetic_failure_sample keeps the failure library gate non-empty on all-green runs.
# 函数用途: 提供一个合成失败样本，证明失败样本合同入口可验收。
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


# LLM: _summary counts phase statuses.
# 函数用途: 生成 passed/failed/total 摘要，只读取结构化 status 字段。
def _summary(phases: tuple[PreRealTaskValidationPhase, ...]) -> dict[str, int]:
    return {
        "failed": sum(phase.status == "FAILED" for phase in phases),
        "passed": sum(phase.status == "PASSED" for phase in phases),
        "total": len(phases),
    }


# LLM: _write_json persists the pre-real validation report.
# 函数用途: 写入 refs-first JSON 报告，供 CLI、CI 或后续审计读取。
def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


__all__ = [
    "PreRealTaskValidationPhase",
    "PreRealTaskValidationReport",
    "PreRealTaskValidationRequest",
    "run_pre_real_task_validation",
]
