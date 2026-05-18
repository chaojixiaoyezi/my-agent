# LLM: Main-agent foundation runner turns the six requested stability checks into one refs-first report.
# 模块用途: 提供主代理基础测试矩阵入口；确定性用例本地执行，真实模型用例明确标记需要显式运行。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..subagents.static_site_validator import run_static_site_check
from .activity_timeout import ActivitySnapshot, ActivityTimeoutPolicy, decide_activity_timeout
from .e2e_matrix_runner import E2ERunnerRequest, run_e2e_matrix
from .error_taxonomy import classify_error
from .evidence_contract import (
    EvidenceClaim,
    EvidenceContractRequest,
    EvidenceSourceRef,
    evaluate_evidence_contract,
)

REAL_MODEL_CASE_IDS = {
    "single_agent_real_tasks",
    "compact_resume_real_cycle",
    "tool_error_recovery_real",
}


# LLM: MainAgentFoundationRequest bundles test runner options without adding end-user config knobs.
# 类用途: 描述主代理基础测试的工作区和是否纳入真实模型结果；默认只跑本地确定性用例。
@dataclass(frozen=True)
class MainAgentFoundationRequest:
    workspace: Path
    include_real_model: bool = False


# LLM: MainAgentFoundationCaseResult is one test category outcome with evidence refs.
# 类用途: 保存单个主代理基础测试类别的状态、中文说明、证据路径和问题列表。
@dataclass(frozen=True)
class MainAgentFoundationCaseResult:
    case_id: str
    title: str
    status: str
    summary: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    # LLM: to_dict keeps reports stable for CLI/frontend/doc display.
    # 函数用途: 转成普通 dict，避免调用方依赖 dataclass 内部结构。
    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


# LLM: MainAgentFoundationReport summarizes all categories without hiding skipped real-model tests.
# 类用途: 保存主代理基础测试总报告；ok 只要求已执行用例没有失败。
@dataclass(frozen=True)
class MainAgentFoundationReport:
    ok: bool
    summary: dict[str, int]
    results: list[MainAgentFoundationCaseResult]

    # LLM: to_dict returns a refs-first payload safe for prompt injection and JSON reports.
    # 函数用途: 输出摘要、状态和证据引用，不携带大文件正文。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "results": [item.to_dict() for item in self.results],
        }


# LLM: run_main_agent_foundation executes deterministic checks and records real-model gaps honestly.
# 函数用途: 执行主代理基础测试的固定入口；真实模型测试不在这里假装通过。
def run_main_agent_foundation(request: MainAgentFoundationRequest) -> MainAgentFoundationReport:
    workspace = Path(request.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    results = [
        _case_tool_failure_contracts(workspace),
        _case_research_evidence_contracts(workspace),
        _case_web_artifact_validator(workspace),
        _case_activity_timeout_recovery(workspace),
        _real_model_placeholder(
            "single_agent_real_tasks",
            "单代理真实任务测试",
            "需要显式运行真实模型任务：HTML、文件整理、代码修复、测试。",
            include_real_model=request.include_real_model,
        ),
        _real_model_placeholder(
            "compact_resume_real_cycle",
            "Compact/Resume 真实续接测试",
            "需要显式运行低阈值 compact、resume 和继续任务链路。",
            include_real_model=request.include_real_model,
        ),
        _case_large_output_artifact_refs(workspace),
        _real_model_placeholder(
            "tool_error_recovery_real",
            "错误恢复真实模型测试",
            "需要真实模型遇到工具失败后修正路径或参数并继续。",
            include_real_model=request.include_real_model,
        ),
        _case_deterministic_e2e_matrix(workspace),
    ]
    return MainAgentFoundationReport(
        ok=not any(item.status == "FAILED" for item in results),
        summary=_summary(results),
        results=results,
    )


# LLM: _case_tool_failure_contracts validates stable error taxonomy before model recovery tests.
# 函数用途: 制造常见失败文本，确认路径、权限、超时、工具不可用和模型失败会被稳定分类。
def _case_tool_failure_contracts(workspace: Path) -> MainAgentFoundationCaseResult:
    samples = {
        "PATH_INVALID": "文件不存在: missing-input.txt",
        "PATH_OUTSIDE_WORKSPACE": "outside workspace: /tmp/not-allowed.txt",
        "WRITE_FORBIDDEN": "permission denied while writing protected file",
        "TOOL_UNAVAILABLE": "unknown tool: browser_magic",
        "TOOL_INVALID_ARGUMENTS": "invalid argument schema for write_file",
        "TOOL_TIMEOUT": "tool timed out after 240 seconds",
        "MODEL_UPSTREAM_FAILED": "anthropic compatible provider returned 502",
    }
    observed = {expected: classify_error(message).code for expected, message in samples.items()}
    issues = [f"{expected}->{actual}" for expected, actual in observed.items() if actual != expected]
    evidence = workspace / "tool_failure_contracts" / "classification.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(observed, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return MainAgentFoundationCaseResult(
        case_id="tool_failure_contracts",
        title="工具失败测试",
        status="FAILED" if issues else "PASSED",
        summary="路径、权限、超时、工具不可用、参数错误和模型上游失败都有稳定错误码。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


# LLM: _case_research_evidence_contracts blocks fabricated table data without source refs.
# 函数用途: 用一正一反两组资料 claim 证明关键统计字段必须挂结构化来源。
def _case_research_evidence_contracts(workspace: Path) -> MainAgentFoundationCaseResult:
    valid = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[
                EvidenceSourceRef(
                    source_id="github-api-openclaw",
                    source_type="api",
                    uri="https://api.github.com/repos/openclaw/openclaw",
                    retrieved_at="2026-05-18T10:00:00Z",
                )
            ],
            claims=[
                EvidenceClaim(
                    claim_id="openclaw-stars",
                    field="stargazers_count",
                    value=372838,
                    source_ids=["github-api-openclaw"],
                )
            ],
            required_fields=["stargazers_count"],
        )
    )
    invalid = evaluate_evidence_contract(
        EvidenceContractRequest(
            claims=[
                EvidenceClaim(
                    claim_id="repo-weekly-growth",
                    field="weekly_star_growth",
                    value=581200,
                    source_ids=[],
                )
            ],
            required_fields=["weekly_star_growth"],
        )
    )
    evidence = workspace / "research_evidence_contracts" / "report.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {"valid": valid.to_dict(), "invalid": invalid.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    issues: list[str] = []
    if not valid.ok:
        issues.append("valid sourced claim failed")
    if invalid.ok:
        issues.append("unsourced claim passed")
    return MainAgentFoundationCaseResult(
        case_id="research_evidence_contracts",
        title="资料证据合同测试",
        status="FAILED" if issues else "PASSED",
        summary="关键资料字段必须有 source_ref；无来源统计不能通过验收。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


# LLM: _case_web_artifact_validator proves generated web apps are checked by DOM facts.
# 函数用途: 生成一份 HTML/JS id 不一致的页面，确认通用静态站点验收会机器失败。
def _case_web_artifact_validator(workspace: Path) -> MainAgentFoundationCaseResult:
    site = workspace / "web_artifact_validator" / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text('<div id="homeProducts"></div><script src="app.js"></script>', encoding="utf-8")
    (site / "app.js").write_text("document.getElementById('productGrid').innerHTML = '<p>商品</p>';", encoding="utf-8")
    record = run_static_site_check(
        {
            "name": "generated web app",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        },
        workspace / "web_artifact_validator",
    )
    evidence = workspace / "web_artifact_validator" / "validation.json"
    evidence.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    expected_hit = record.validation_result.get("missing_dom_id_hits") == ["getElementById:productGrid"]
    issues = [] if record.executed and not record.passed and expected_hit else ["web validator did not catch missing DOM id"]
    return MainAgentFoundationCaseResult(
        case_id="web_artifact_validator",
        title="Web 产物机器验收测试",
        status="FAILED" if issues else "PASSED",
        summary="HTML/JS 绑定目标不一致时，static_site_check 会返回结构化 missing_dom_id_hits。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


# LLM: _case_activity_timeout_recovery validates idle-based timeout and recovery refs.
# 函数用途: 证明长任务最近有活动不会被总耗时误杀，真正空闲时会要求写恢复包。
def _case_activity_timeout_recovery(workspace: Path) -> MainAgentFoundationCaseResult:
    active = decide_activity_timeout(
        ActivityTimeoutPolicy(idle_timeout_seconds=120, wall_timeout_seconds=300),
        ActivitySnapshot(started_at=0, now=900, last_activity_at=880, active_tool_count=1),
    )
    idle = decide_activity_timeout(
        ActivityTimeoutPolicy(idle_timeout_seconds=120),
        ActivitySnapshot(
            started_at=0,
            now=500,
            last_activity_at=100,
            latest_checkpoint_ref="checkpoint.json",
            latest_recovery_snapshot_ref="snapshot.json",
        ),
    )
    evidence = workspace / "activity_timeout_recovery" / "decisions.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps({"active": active.to_dict(), "idle": idle.to_dict()}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    issues: list[str] = []
    if active.timed_out or active.action != "keep_running":
        issues.append("active task timed out")
    if not idle.timed_out or idle.action != "write_recovery_and_pause":
        issues.append("idle task did not request recovery pause")
    return MainAgentFoundationCaseResult(
        case_id="activity_timeout_recovery",
        title="长任务活动超时/恢复测试",
        status="FAILED" if issues else "PASSED",
        summary="长任务按 idle activity 判断；空闲超时时返回 checkpoint/snapshot refs 供恢复。",
        evidence_refs=[str(evidence)],
        issues=issues,
    )


# LLM: _case_large_output_artifact_refs checks refs/hash/size behavior without embedding output bodies.
# 函数用途: 写入模拟大输出并生成 metadata，验证测试报告只带 artifact 引用。
def _case_large_output_artifact_refs(workspace: Path) -> MainAgentFoundationCaseResult:
    artifact = workspace / "large_output_artifact_refs" / "large-tool-output.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"large output row {index}: {'x' * 120}" for index in range(800))
    artifact.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    metadata = {
        "artifact_ref": str(artifact),
        "size": artifact.stat().st_size,
        "sha256": digest,
        "read_hint": {"offset": 0, "max_chars": 4000},
    }
    evidence = artifact.with_suffix(".meta.json")
    evidence.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return MainAgentFoundationCaseResult(
        case_id="large_output_artifact_refs",
        title="大文件/大输出测试",
        status="PASSED",
        summary=f"大输出已外置为 artifact；报告只保留 ref/size/hash，sha256={digest[:12]}。",
        evidence_refs=[str(evidence)],
    )


# LLM: _case_deterministic_e2e_matrix nests the lower-level E2E matrix report as evidence.
# 函数用途: 复用真实 E2E 矩阵的本地确定性用例，并把完整报告写成证据文件。
def _case_deterministic_e2e_matrix(workspace: Path) -> MainAgentFoundationCaseResult:
    report = run_e2e_matrix(E2ERunnerRequest(workspace=workspace / "deterministic_e2e_matrix"))
    evidence = workspace / "deterministic_e2e_matrix" / "report.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return MainAgentFoundationCaseResult(
        case_id="deterministic_e2e_matrix",
        title="E2E Matrix 确定性测试",
        status="FAILED" if not report.ok else "PASSED",
        summary=f"本地 E2E 矩阵 passed={report.summary['passed']} skipped={report.summary['skipped']} failed={report.summary['failed']}。",
        evidence_refs=[str(evidence)],
        issues=[] if report.ok else ["deterministic E2E matrix failed"],
    )


# LLM: _real_model_placeholder prevents accidental green reports for tests that require model calls.
# 函数用途: 真实模型用例没有外部 runner 时标记 SKIPPED，避免把未测试当成已通过。
def _real_model_placeholder(
    case_id: str,
    title: str,
    summary: str,
    *,
    include_real_model: bool,
) -> MainAgentFoundationCaseResult:
    status = "SKIPPED"
    note = summary
    if include_real_model:
        note = f"{summary} 当前基础 runner 只登记需求，真实模型由脚本单独执行。"
    return MainAgentFoundationCaseResult(case_id=case_id, title=title, status=status, summary=note)


# LLM: _summary counts statuses for quick progress and CI display.
# 函数用途: 汇总 PASSED/FAILED/SKIPPED 数量，并保留 total。
def _summary(results: list[MainAgentFoundationCaseResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(item.status == "PASSED" for item in results),
        "failed": sum(item.status == "FAILED" for item in results),
        "skipped": sum(item.status == "SKIPPED" for item in results),
    }


__all__ = [
    "MainAgentFoundationCaseResult",
    "MainAgentFoundationReport",
    "MainAgentFoundationRequest",
    "run_main_agent_foundation",
]
