# LLM: Small real acceptance runner executes bounded wrapper probes before larger real tasks.
# 模块用途: 运行小型真实验收批次，证明 ToolRegistry wrapper、Shadow runtime 和小真实闸门能闭环。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .failure_sample_capture import failure_samples_from_case_results
from .real_tool_dry_run_contract import validate_real_tool_dry_run_probes
from .shadow_mode_runtime_contract import validate_shadow_mode_runtime
from .small_real_acceptance_gate import validate_small_real_acceptance_gate
from .small_real_acceptance_probes import (
    controlled_exec_grant,
    controlled_exec_probe,
    read_file_probe,
    shadow_runtime_facts,
    small_real_registry,
)


# LLM: SmallRealAcceptanceRunRequest keeps runner inputs small and explicit.
# 类用途: 描述小真实验收工作区和可选 case 过滤条件。
@dataclass(frozen=True)
class SmallRealAcceptanceRunRequest:
    workspace: Path
    case_ids: tuple[str, ...] = ()


# LLM: SmallRealCaseResult is the refs-first result for one bounded case.
# 类用途: 保存单个小真实 case 的状态、artifact refs、verification refs 和 issues。
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

    # LLM: to_dict serializes one case without embedding tool or artifact bodies.
    # 函数用途: 输出小真实 case 的 JSON 形状，只携带 refs 和结构化问题。
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


# LLM: SmallRealAcceptanceRunReport summarizes all small-real cases.
# 类用途: 保存小真实批次报告、闸门错误码和失败样本引用。
@dataclass(frozen=True)
class SmallRealAcceptanceRunReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    gate_error_codes: tuple[str, ...]
    cases: tuple[SmallRealCaseResult, ...]
    failure_samples: tuple[dict[str, Any], ...] = ()

    # LLM: to_dict keeps the runner report stable for files, CLI, and Card Runtime.
    # 函数用途: 转换小真实批次报告，避免调用方依赖 dataclass 内部结构。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "gate_error_codes": list(self.gate_error_codes),
            "cases": [case.to_dict() for case in self.cases],
            "failure_samples": list(self.failure_samples),
        }


# LLM: _CaseOutcome carries intermediate case facts into result builders.
# 类用途: 把 case 状态、路径和 refs 打包，避免 helper 参数变宽。
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


# LLM: run_small_real_acceptance runs deterministic live-wrapper probes only.
# 函数用途: 写隔离工作区、执行只读/dry-run wrapper、生成 refs-first 报告和失败样本入口。
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
        _run_shadow_mode_runtime(run_context),
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
    _write_json(workspace / report.report_ref, report.to_dict())
    return report


# LLM: _SmallRunContext stores shared workspace state for ordered small cases.
# 类用途: 保存 ToolRegistry 和已通过的 probe refs，供 Shadow runtime case 使用。
class _SmallRunContext:
    # LLM: __init__ initializes the local registry and probe ledger.
    # 函数用途: 绑定小真实工作区，创建只在该工作区内执行的工具注册表。
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.registry = small_real_registry(workspace)
        self.probe_refs: list[dict[str, object]] = []


# LLM: _run_real_read_file proves the read_file wrapper path before larger tasks.
# 函数用途: 写入隔离输入文件并通过 ToolRegistry 执行真实只读 read_file。
def _run_real_read_file(context: _SmallRunContext) -> SmallRealCaseResult:
    case_dir = _case_dir(context.workspace, "real_read_file")
    input_path = case_dir / "input.txt"
    input_path.write_text("hello from real read_file wrapper\n", encoding="utf-8")
    result = context.registry.execute_call(
        {"tool": "read_file", "path": _rel(input_path, context.workspace)},
        allowed_tools=["read_file"],
    )
    probe = read_file_probe(result)
    return _tool_probe_case(context, "real_read_file", case_dir, probe)


# LLM: _run_controlled_exec_dry_run proves shell gateway planning without execution.
# 函数用途: 使用父级 grant 调用 controlled_exec dry-run，不执行真实 shell 副作用。
def _run_controlled_exec_dry_run(context: _SmallRunContext) -> SmallRealCaseResult:
    case_dir = _case_dir(context.workspace, "controlled_exec_dry_run")
    result = context.registry.execute_call(
        {"tool": "controlled_exec", "command": "pwd", "apply": False, "cwd": _rel(case_dir, context.workspace)},
        allowed_tools=["controlled_exec"],
        write_boundary={"controlled_exec_grants": [controlled_exec_grant(case_dir)]},
    )
    probe = controlled_exec_probe(result)
    return _tool_probe_case(context, "controlled_exec_dry_run", case_dir, probe)


# LLM: _tool_probe_case validates and records one real tool probe.
# 函数用途: 将 probe、verification 和 gate case 统一写成 refs-first case result。
def _tool_probe_case(
    context: _SmallRunContext,
    case_id: str,
    case_dir: Path,
    probe: dict[str, object],
) -> SmallRealCaseResult:
    validation = validate_real_tool_dry_run_probes((probe,))
    probe_ref = _write_json(case_dir / "probe.json", probe, root=context.workspace)
    verification_ref = _write_json(case_dir / "verification.json", _validation_dict(validation), root=context.workspace)
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


# LLM: _run_shadow_mode_runtime proves phase-6 runtime uses phase-5 probe refs.
# 函数用途: 构造影子模式运行事实并验证它没有真实副作用。
def _run_shadow_mode_runtime(context: _SmallRunContext) -> SmallRealCaseResult:
    case_dir = _case_dir(context.workspace, "shadow_mode_runtime")
    comparison_ref = _write_json(case_dir / "human-review.json", {"agreement": True}, root=context.workspace)
    facts = shadow_runtime_facts(context.probe_refs, comparison_ref)
    validation = validate_shadow_mode_runtime(facts)
    facts_ref = _write_json(case_dir / "shadow-runtime.json", facts, root=context.workspace)
    verification_ref = _write_json(case_dir / "verification.json", _validation_dict(validation), root=context.workspace)
    return _case_result(
        context.workspace,
        _CaseOutcome(
            case_id="shadow_mode_runtime",
            status="PASSED" if validation.ok else "FAILED",
            case_dir=case_dir,
            artifact_refs=[facts_ref, comparison_ref],
            verification_refs=[verification_ref],
            tool_probe_refs=[str(item["contract_ref"]) for item in context.probe_refs],
            issues=list(validation.error_codes),
        ),
    )


# LLM: _case_result converts one outcome into the public case result.
# 函数用途: 统一填充 workspace_ref、gate_case 和 refs，不解析自然语言说明。
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


# LLM: _gate_case builds the small-real gate payload from structured outcome facts.
# 函数用途: 生成小真实闸门所需字段，固定只读/dry-run 和隔离边界。
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


# LLM: _case_dir allocates one deterministic isolated case directory.
# 函数用途: 生成并创建 case 专属目录，避免不同验收用例互相覆盖产物。
def _case_dir(workspace: Path, case_id: str) -> Path:
    path = workspace / "small_real_acceptance" / "cases" / case_id
    path.mkdir(parents=True, exist_ok=True)
    return path


# LLM: _write_json writes bounded structured evidence and returns a relative ref.
# 函数用途: 把 probe、verification 或 report 写入工作区，并返回可放进报告的相对路径。
def _write_json(path: Path, payload: object, *, root: Path | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return _rel(path, root) if root else ""


# LLM: _validation_dict serializes OfflineContractValidation.
# 函数用途: 将合同校验结果转成稳定 JSON 结构，方便 replay 和人工审计。
def _validation_dict(validation) -> dict[str, object]:
    return {
        "ok": validation.ok,
        "error_codes": list(validation.error_codes),
        "findings": list(validation.findings),
    }


# LLM: _rel makes refs workspace-relative when a root is available.
# 函数用途: 生成报告使用的相对路径引用，避免把机器本地绝对路径扩散给上层。
def _rel(path: Path, root: Path | None) -> str:
    return str(path.resolve().relative_to(root.resolve())) if root else str(path)


# LLM: _summary counts small-real case statuses.
# 函数用途: 生成 passed/failed/total 摘要，不从 case summary 文本判断状态。
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
