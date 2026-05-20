# LLM: Real task acceptance validates produced artifacts from structured contracts only.
# 模块用途: 读取 expected_artifacts.json 的机器字段，验收主代理真实任务产物并写 refs-first 报告。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..tooling.file_write_session_inspection import open_file_write_sessions
from .artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from .artifact_candidate_paths import report_with_candidate_paths
from .staged_checkpoint_acceptance import artifact_path as staged_artifact_path
from .staged_checkpoint_acceptance import staged_checkpoint_findings


# LLM: RealTaskAcceptanceRequest bundles the files needed to validate one executed case.
# 类用途: 描述验收一个真实任务所需的期望产物合同、任务工作区和报告输出路径。
@dataclass(frozen=True)
class RealTaskAcceptanceRequest:
    expected_artifacts_path: Path
    task_workspace: Path
    report_path: Path


# LLM: RealTaskArtifactAcceptance stores one artifact validation outcome.
# 类用途: 保存一个 expected artifact 的路径、validator、通过状态和通用验收报告。
@dataclass(frozen=True)
class RealTaskArtifactAcceptance:
    artifact_id: str
    path: str
    validator: str
    ok: bool
    report: dict[str, object]

    # LLM: to_dict keeps artifact acceptance easy to persist and compare.
    # 函数用途: 转成 JSON 字段，供总报告和后续 repair worker 读取。
    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "validator": self.validator,
            "ok": self.ok,
            "report": dict(self.report),
        }


# LLM: RealTaskAcceptanceReport summarizes all artifact checks for one case.
# 类用途: 保存一个真实任务的产物验收结果；报告里只放路径、summary 和 findings。
@dataclass(frozen=True)
class RealTaskAcceptanceReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    artifacts: list[RealTaskArtifactAcceptance] = field(default_factory=list)
    runtime_findings: list[dict[str, object]] = field(default_factory=list)

    # LLM: to_dict emits a stable machine report for the execution runner.
    # 函数用途: 转成 JSON，便于 CLI 和未来 Card Runtime 读取验收结果。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "runtime_findings": [dict(finding) for finding in self.runtime_findings],
        }


# LLM: validate_real_task_artifacts is the public post-run acceptance entrypoint.
# 函数用途: 按 expected_artifacts.json 中的 preferred_path 和 validator 验收产物，不读 prompt 文本。
def validate_real_task_artifacts(request: RealTaskAcceptanceRequest) -> RealTaskAcceptanceReport:
    expected = _expected_artifacts(request.expected_artifacts_path)
    artifacts = [
        _validate_artifact_item(item, request.task_workspace)
        for item in expected
        if item.get("required") is not False
    ]
    runtime_findings = [
        *_staged_checkpoint_findings(expected, request.task_workspace),
        *_runtime_findings(request.task_workspace, artifacts),
    ]
    report = RealTaskAcceptanceReport(
        ok=all(item.ok for item in artifacts) and not runtime_findings,
        summary=_summary(artifacts, runtime_findings),
        report_ref=str(request.report_path),
        artifacts=artifacts,
        runtime_findings=runtime_findings,
    )
    _write_report(request.report_path, report.to_dict())
    return report


# LLM: _expected_artifacts reads only structured artifact contracts.
# 函数用途: 从 expected_artifacts.json 取 artifact 列表；缺失或坏 JSON 时返回一个缺失占位项。
def _expected_artifacts(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [_missing_contract_item(path)]
    items = payload.get("artifacts") if isinstance(payload, dict) else None
    return [dict(item) for item in items] if isinstance(items, list) else []


# LLM: _validate_artifact_item dispatches one expected artifact through the generic validator.
# 函数用途: 根据 preferred_path 定位产物，并把 validation_contract.validator 记录进报告。
def _validate_artifact_item(
    item: dict[str, object],
    task_workspace: Path,
) -> RealTaskArtifactAcceptance:
    artifact_id = str(item.get("artifact_id") or "")
    path = _artifact_path(item, task_workspace)
    validator = _validator_name(item)
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=task_workspace,
            validation_contract=_validation_contract(item),
        )
    )
    report = report_with_candidate_paths(
        report,
        path,
        task_workspace,
        validation_contract=_validation_contract(item),
    ).to_dict()
    return RealTaskArtifactAcceptance(
        artifact_id=artifact_id,
        path=str(path),
        validator=validator,
        ok=bool(report.get("ok")),
        report=report,
    )


# LLM: _artifact_path resolves preferred_path inside the task workspace.
# 函数用途: 把结构化 preferred_path 转成绝对路径，拒绝把相对路径解析到任务目录外。
def _artifact_path(item: dict[str, object], task_workspace: Path) -> Path:
    return staged_artifact_path(str(item.get("preferred_path") or ""), task_workspace)


# LLM: _staged_checkpoint_findings validates machine-declared intermediate outputs.
# 函数用途: 检查 staging_contract.checkpoint_refs，避免长任务只到最后才发现数据为空或脚本缺失。
def _staged_checkpoint_findings(
    items: list[dict[str, object]],
    task_workspace: Path,
) -> list[dict[str, object]]:
    return staged_checkpoint_findings(items, task_workspace)


# LLM: _validator_name extracts display metadata, not behavior branching.
# 函数用途: 从 validation_contract 记录 validator 名称；当前行为统一走 Artifact Acceptance。
def _validator_name(item: dict[str, object]) -> str:
    contract = item.get("validation_contract")
    if not isinstance(contract, dict):
        return "artifact_acceptance"
    return str(contract.get("validator") or "artifact_acceptance")


# LLM: _validation_contract passes structured expected-artifact options to generic validators.
# 函数用途: 从 expected_artifacts.json 取 validation_contract；验收行为不再只看文件后缀。
def _validation_contract(item: dict[str, object]) -> dict[str, object]:
    contract = item.get("validation_contract")
    return dict(contract) if isinstance(contract, dict) else {}


# LLM: _runtime_findings adds non-artifact machine facts that still block completion.
# 函数用途: 检查真实任务工作区里的运行时合同问题，例如未 finish 的分块写入会话。
def _runtime_findings(
    task_workspace: Path,
    artifacts: list[RealTaskArtifactAcceptance],
) -> list[dict[str, object]]:
    accepted_targets = _accepted_artifact_targets(artifacts)
    return [
        _open_session_finding(session)
        for session in open_file_write_sessions(task_workspace, limit=20)
        if _session_target(session) not in accepted_targets
    ]


def _accepted_artifact_targets(artifacts: list[RealTaskArtifactAcceptance]) -> set[str]:
    return {str(Path(item.path).resolve()) for item in artifacts if item.ok}


def _session_target(session: dict[str, object]) -> str:
    target = session.get("target_path")
    if not isinstance(target, dict):
        return ""
    value = target.get("resolved") or target.get("raw")
    if not value:
        return ""
    return str(Path(str(value)).resolve())


# LLM: _open_session_finding turns a write-session manifest summary into a stable acceptance finding.
# 函数用途: 将 open file_write_session 作为机器验收失败项记录，避免超时后误判产物已完成。
def _open_session_finding(session: dict[str, object]) -> dict[str, object]:
    return {
        "code": "OPEN_FILE_WRITE_SESSION",
        "severity": "hard",
        "session_id": str(session.get("session_id") or ""),
        "target_path": session.get("target_path") or {},
        "manifest_path": str(session.get("manifest_path") or ""),
        "preview_path": str(session.get("preview_path") or ""),
        "preview_materialized": bool(session.get("preview_materialized")),
        "received_chunks": list(session.get("received_chunks") or []),
        "next_chunk_index": int(session.get("next_chunk_index") or 0),
        "continue_tool_call": session.get("continue_tool_call") or {},
        "finish_tool_call": session.get("finish_tool_call") or {},
        "resume_action": "append_from_next_chunk_then_finish",
        "chunk_content_read_required": False,
        "existing_chunks_authoritative": True,
    }


# LLM: _summary counts artifact and runtime contract outcomes for case-level status.
# 函数用途: 生成 passed/failed/total 汇总，执行器据此决定任务是否真正完成。
def _summary(
    artifacts: list[RealTaskArtifactAcceptance],
    runtime_findings: list[dict[str, object]],
) -> dict[str, int]:
    failed = sum(not item.ok for item in artifacts)
    return {
        "total": len(artifacts) + len(runtime_findings),
        "passed": len(artifacts) - failed,
        "failed": failed + len(runtime_findings),
    }


# LLM: _missing_contract_item turns a missing contract into a normal missing artifact report.
# 函数用途: expected_artifacts.json 缺失时也输出可验收失败，而不是抛异常中断报告。
def _missing_contract_item(path: Path) -> dict[str, object]:
    return {
        "artifact_id": "expected_artifacts_contract",
        "preferred_path": str(path),
        "validation_contract": {"validator": "artifact_acceptance"},
        "required": True,
    }


# LLM: _write_report writes deterministic JSON for acceptance refs.
# 函数用途: 写真实任务验收报告，字段排序便于 diff 和后续审计。
def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


__all__ = [
    "RealTaskAcceptanceReport",
    "RealTaskAcceptanceRequest",
    "RealTaskArtifactAcceptance",
    "validate_real_task_artifacts",
]
