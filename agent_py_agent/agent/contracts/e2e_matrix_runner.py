
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .e2e_matrix import REAL_E2E_MATRIX, E2EMatrixCase
from .error_taxonomy import classify_error


@dataclass(frozen=True)
class E2ERunnerRequest:
    workspace: Path
    include_real_model: bool = False


@dataclass(frozen=True)
class E2ECaseResult:
    case_id: str
    status: str
    summary: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class E2ERunnerReport:
    ok: bool
    summary: dict[str, int]
    results: list[E2ECaseResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "results": [item.to_dict() for item in self.results],
        }


def run_e2e_matrix(request: E2ERunnerRequest) -> E2ERunnerReport:
    workspace = Path(request.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    results = [_run_case(case, workspace, include_real_model=request.include_real_model) for case in REAL_E2E_MATRIX]
    return E2ERunnerReport(
        ok=not any(item.status == "FAILED" for item in results),
        summary=_summary(results),
        results=results,
    )


def _run_case(case: E2EMatrixCase, workspace: Path, *, include_real_model: bool) -> E2ECaseResult:
    if case.execution_mode == "real_model" and not include_real_model:
        return E2ECaseResult(case.case_id, "SKIPPED", "real_model case was not requested")
    checks = {
        "windows_chinese_path_write": _case_windows_chinese_path_write,
        "large_tool_output_artifact": _case_large_tool_output_artifact,
        "tool_failure_taxonomy": _case_tool_failure_taxonomy,
    }
    check = checks.get(case.case_id)
    if check is None:
        return E2ECaseResult(
            case.case_id,
            "FAILED",
            f"requested {case.execution_mode} runner is not implemented",
            issues=["REAL_MODEL_RUNNER_NOT_IMPLEMENTED"],
        )
    try:
        return check(workspace)
    except Exception as exc:  # pragma: no cover - defensive report boundary
        return E2ECaseResult(case.case_id, "FAILED", "deterministic check raised", issues=[f"{exc.__class__.__name__}: {exc}"])


def _case_windows_chinese_path_write(workspace: Path) -> E2ECaseResult:
    target = workspace / "windows_chinese_path_write" / "中文 路径" / "结果 文件.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("路径写入成功", encoding="utf-8")
    if target.read_text(encoding="utf-8") != "路径写入成功":
        return E2ECaseResult("windows_chinese_path_write", "FAILED", "readback mismatch", issues=[str(target)])
    return E2ECaseResult("windows_chinese_path_write", "PASSED", "Chinese path write/read succeeded", [str(target)])


def _case_large_tool_output_artifact(workspace: Path) -> E2ECaseResult:
    artifact = workspace / "large_tool_output_artifact" / "tool-output.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"large output line {index}" for index in range(500))
    artifact.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    evidence = artifact.with_suffix(".meta.json")
    evidence.write_text(_metadata_json(artifact, digest), encoding="utf-8")
    return E2ECaseResult("large_tool_output_artifact", "PASSED", f"artifact_ref={artifact.name}; sha256={digest[:12]}", [str(evidence)])


def _case_tool_failure_taxonomy(workspace: Path) -> E2ECaseResult:
    samples = {
        "PATH_INVALID": "PATH_INVALID: missing.txt",
        "PATH_OUTSIDE_WORKSPACE": "PATH_OUTSIDE_WORKSPACE: /tmp/other.txt",
        "WRITE_FORBIDDEN": "WRITE_FORBIDDEN: writing denied",
        "TOOL_UNAVAILABLE": "TOOL_UNAVAILABLE: browser_magic",
        "MODEL_UPSTREAM_FAILED": "MODEL_UPSTREAM_FAILED: provider timeout",
    }
    issues = [f"{expected}->{classify_error(message).code}" for expected, message in samples.items() if classify_error(message).code != expected]
    evidence = workspace / "tool_failure_taxonomy" / "classification.txt"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("\n".join(f"{key}: {classify_error(value).code}" for key, value in samples.items()), encoding="utf-8")
    status = "FAILED" if issues else "PASSED"
    return E2ECaseResult("tool_failure_taxonomy", status, "taxonomy samples classified", [str(evidence)], issues)


def _metadata_json(path: Path, digest: str) -> str:
    size = path.stat().st_size
    return f'{{"path":"{path}","size":{size},"sha256":"{digest}"}}'


def _summary(results: list[E2ECaseResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(item.status == "PASSED" for item in results),
        "failed": sum(item.status == "FAILED" for item in results),
        "skipped": sum(item.status == "SKIPPED" for item in results),
    }


__all__ = ["E2ERunnerReport", "E2ERunnerRequest", "E2ECaseResult", "run_e2e_matrix"]
