# LLM: Artifact candidate path helpers keep alternate-path repair facts contract-based.
# 模块用途: 扫描同名候选产物，并用同一份验收合同判断是否能提示恢复流程复用。

from __future__ import annotations

from pathlib import Path

from .artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from .artifact_acceptance_models import ArtifactAcceptanceReport, ArtifactFinding


# LLM: report_with_candidate_paths adds structured repair hints without accepting wrong paths.
# 函数用途: 当目标产物缺失时，扫描同名候选文件并写入 finding，方便恢复包纠正路径。
def report_with_candidate_paths(
    report: ArtifactAcceptanceReport,
    expected_path: Path,
    task_workspace: Path,
    *,
    validation_contract: dict[str, object],
) -> ArtifactAcceptanceReport:
    if report.ok or expected_path.exists():
        return report
    candidates = _candidate_artifact_paths(expected_path, task_workspace)
    if not candidates:
        return report
    finding = _candidate_path_finding(candidates[0], expected_path, task_workspace, validation_contract)
    return ArtifactAcceptanceReport(
        ok=False,
        artifact_ref=report.artifact_ref,
        artifact_kind=report.artifact_kind,
        findings=[*report.findings, finding],
    )


# LLM: _candidate_artifact_paths searches by filename under the task workspace only.
# 函数用途: 找同名候选产物，排除目标路径本身和内部元数据目录，避免读取自然语言日志。
def _candidate_artifact_paths(expected_path: Path, task_workspace: Path) -> list[Path]:
    name = expected_path.name
    if not name:
        return []
    root = task_workspace.resolve()
    expected = expected_path.resolve(strict=False)
    candidates: list[Path] = []
    for candidate in root.rglob(name):
        resolved = candidate.resolve(strict=False)
        if resolved != expected and not _is_internal_path(resolved, root):
            candidates.append(resolved)
    return sorted(candidates, key=lambda item: str(item))


# LLM: _candidate_path_finding validates alternate artifacts before suggesting reuse.
# 函数用途: 对同名候选产物复用同一份 validation_contract，避免错表或空壳误导恢复流程。
def _candidate_path_finding(
    candidate: Path,
    expected_path: Path,
    task_workspace: Path,
    validation_contract: dict[str, object],
) -> ArtifactFinding:
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=candidate,
            workspace_root=task_workspace,
            validation_contract=validation_contract,
        )
    )
    if report.ok and not report.findings:
        return ArtifactFinding(
            code="ARTIFACT_CANDIDATE_PATH",
            severity="soft",
            message="A file with the expected artifact name exists at another workspace path.",
            location=str(expected_path),
            value=str(candidate),
        )
    return ArtifactFinding(
        code="ARTIFACT_CANDIDATE_REJECTED",
        severity="soft",
        message="A same-name artifact exists at another path but does not satisfy the validation contract.",
        location=str(candidate),
        value=",".join(finding.code for finding in report.findings),
    )


# LLM: _is_internal_path keeps caches and run metadata out of artifact candidate facts.
# 函数用途: 判断候选路径是否位于内部目录，避免把日志、session 临时文件当成业务产物。
def _is_internal_path(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part.startswith(".my_agent") or part in {"memory_archive", "__pycache__"} for part in rel_parts)


__all__ = ["report_with_candidate_paths"]
