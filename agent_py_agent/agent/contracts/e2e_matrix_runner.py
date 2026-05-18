# LLM: Deterministic E2E matrix runner turns scenario contracts into local smoke checks.
# 模块用途: 执行不调用模型的真实端到端矩阵第一片，并把真实模型用例明确标为 SKIPPED。

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .e2e_matrix import REAL_E2E_MATRIX, E2EMatrixCase
from .error_taxonomy import classify_error


# LLM: E2ERunnerRequest bundles runner options without adding user-facing config sprawl.
# 类用途: 描述 E2E 矩阵执行工作区和是否纳入真实模型用例；默认只跑 deterministic。
@dataclass(frozen=True)
class E2ERunnerRequest:
    workspace: Path
    include_real_model: bool = False


# LLM: E2ECaseResult is a refs-first result for one matrix scenario.
# 类用途: 保存单个 E2E 用例状态、摘要、证据引用和问题列表，不内嵌大正文。
@dataclass(frozen=True)
class E2ECaseResult:
    case_id: str
    status: str
    summary: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    # LLM: to_dict keeps reports JSON/CLI friendly.
    # 函数用途: 将单个用例结果转成普通 dict，方便 CLI、前端或文档报告读取。
    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


# LLM: E2ERunnerReport summarizes matrix execution without hiding skipped real-model cases.
# 类用途: 保存 E2E 矩阵运行摘要和每个用例结果；ok 只要求已执行用例没有失败。
@dataclass(frozen=True)
class E2ERunnerReport:
    ok: bool
    summary: dict[str, int]
    results: list[E2ECaseResult]

    # LLM: to_dict makes the report stable for future CLI/frontend display.
    # 函数用途: 输出 refs-first 报告，不把 artifact 正文写进返回值。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "results": [item.to_dict() for item in self.results],
        }


# LLM: run_e2e_matrix executes deterministic checks and records real-model cases as explicit skips.
# 函数用途: 跑真实 E2E 矩阵第一片；本函数不调用模型、不自动修复、不阻断业务流程。
def run_e2e_matrix(request: E2ERunnerRequest) -> E2ERunnerReport:
    workspace = Path(request.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    results = [_run_case(case, workspace, include_real_model=request.include_real_model) for case in REAL_E2E_MATRIX]
    return E2ERunnerReport(
        ok=not any(item.status == "FAILED" for item in results),
        summary=_summary(results),
        results=results,
    )


# LLM: _run_case dispatches by matrix id while keeping real-model cases non-mutating for now.
# 函数用途: 执行单个用例；没有真实模型 runner 时明确 SKIPPED，避免假装测过。
def _run_case(case: E2EMatrixCase, workspace: Path, *, include_real_model: bool) -> E2ECaseResult:
    if case.execution_mode == "real_model" and not include_real_model:
        return E2ECaseResult(case.case_id, "SKIPPED", "real_model case requires an explicit runner")
    checks = {
        "windows_chinese_path_write": _case_windows_chinese_path_write,
        "large_tool_output_artifact": _case_large_tool_output_artifact,
        "tool_failure_taxonomy": _case_tool_failure_taxonomy,
    }
    check = checks.get(case.case_id)
    if check is None:
        return E2ECaseResult(case.case_id, "SKIPPED", f"{case.execution_mode} runner not implemented yet")
    try:
        return check(workspace)
    except Exception as exc:  # pragma: no cover - defensive report boundary
        return E2ECaseResult(case.case_id, "FAILED", "deterministic check raised", issues=[f"{exc.__class__.__name__}: {exc}"])


# LLM: _case_windows_chinese_path_write validates structured paths with Chinese and spaces.
# 函数用途: 在测试工作区写入/读取中文路径文件，确认路径事实不用自然语言猜。
def _case_windows_chinese_path_write(workspace: Path) -> E2ECaseResult:
    target = workspace / "windows_chinese_path_write" / "中文 路径" / "结果 文件.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("路径写入成功", encoding="utf-8")
    if target.read_text(encoding="utf-8") != "路径写入成功":
        return E2ECaseResult("windows_chinese_path_write", "FAILED", "readback mismatch", issues=[str(target)])
    return E2ECaseResult("windows_chinese_path_write", "PASSED", "Chinese path write/read succeeded", [str(target)])


# LLM: _case_large_tool_output_artifact validates refs/hash/size without returning large content.
# 函数用途: 写入一份模拟大工具输出和索引，只在报告中返回路径、大小和 hash 证据。
def _case_large_tool_output_artifact(workspace: Path) -> E2ECaseResult:
    artifact = workspace / "large_tool_output_artifact" / "tool-output.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"large output line {index}" for index in range(500))
    artifact.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    evidence = artifact.with_suffix(".meta.json")
    evidence.write_text(_metadata_json(artifact, digest), encoding="utf-8")
    return E2ECaseResult("large_tool_output_artifact", "PASSED", f"artifact_ref={artifact.name}; sha256={digest[:12]}", [str(evidence)])


# LLM: _case_tool_failure_taxonomy verifies common failures map to stable non-unknown codes.
# 函数用途: 覆盖路径、权限、工具不可用和模型上游失败的分类，保证恢复建议不是纯自然语言。
def _case_tool_failure_taxonomy(workspace: Path) -> E2ECaseResult:
    samples = {
        "PATH_INVALID": "文件不存在: missing.txt",
        "PATH_OUTSIDE_WORKSPACE": "outside workspace /tmp/other.txt",
        "WRITE_FORBIDDEN": "permission denied while writing",
        "TOOL_UNAVAILABLE": "unknown tool: browser_magic",
        "MODEL_UPSTREAM_FAILED": "anthropic compatible provider timeout",
    }
    issues = [f"{expected}->{classify_error(message).code}" for expected, message in samples.items() if classify_error(message).code != expected]
    evidence = workspace / "tool_failure_taxonomy" / "classification.txt"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("\n".join(f"{key}: {classify_error(value).code}" for key, value in samples.items()), encoding="utf-8")
    status = "FAILED" if issues else "PASSED"
    return E2ECaseResult("tool_failure_taxonomy", status, "taxonomy samples classified", [str(evidence)], issues)


# LLM: _metadata_json keeps artifact evidence small and parseable without importing a JSON helper.
# 函数用途: 生成最小 metadata JSON 字符串，避免报告携带大工具输出正文。
def _metadata_json(path: Path, digest: str) -> str:
    size = path.stat().st_size
    return f'{{"path":"{path}","size":{size},"sha256":"{digest}"}}'


# LLM: _summary counts statuses for quick CLI/frontend display.
# 函数用途: 汇总 PASSED/FAILED/SKIPPED 数量，并保留 total。
def _summary(results: list[E2ECaseResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(item.status == "PASSED" for item in results),
        "failed": sum(item.status == "FAILED" for item in results),
        "skipped": sum(item.status == "SKIPPED" for item in results),
    }


__all__ = ["E2ERunnerReport", "E2ERunnerRequest", "E2ECaseResult", "run_e2e_matrix"]
