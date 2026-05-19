# LLM: Real task acceptance validates produced artifacts from structured contracts only.
# 模块用途: 读取 expected_artifacts.json 的机器字段，验收主代理真实任务产物并写 refs-first 报告。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..tooling.file_write_session_inspection import open_file_write_sessions
from .artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from .artifact_candidate_paths import report_with_candidate_paths


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
        *_runtime_findings(request.task_workspace),
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
    preferred = Path(str(item.get("preferred_path") or ""))
    if preferred.is_absolute():
        return preferred
    return (task_workspace / preferred).resolve()


# LLM: _staged_checkpoint_findings validates machine-declared intermediate outputs.
# 函数用途: 检查 staging_contract.checkpoint_refs，避免长任务只到最后才发现数据为空或脚本缺失。
def _staged_checkpoint_findings(
    items: list[dict[str, object]],
    task_workspace: Path,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    preferred_paths = {str(item.get("preferred_path") or item.get("path") or "") for item in items}
    for item in items:
        for ref_text in _staging_refs_for_item(item, preferred_paths):
            findings.extend(_one_staged_checkpoint_findings(ref_text, task_workspace))
    return findings


# LLM: _staging_refs_for_item extracts checkpoint refs while filtering final artifact paths.
# 函数用途: 从单个 artifact 的 staging_contract 取需要单独验收的阶段产物 refs。
def _staging_refs_for_item(item: dict[str, object], preferred_paths: set[str]) -> list[str]:
    contract = item.get("validation_contract")
    staging = contract.get("staging_contract") if isinstance(contract, dict) else None
    refs = staging.get("checkpoint_refs") if isinstance(staging, dict) else None
    if not isinstance(refs, list):
        return []
    return [
        ref_text
        for ref in refs
        if (ref_text := str(ref)).strip() and ref_text not in preferred_paths
    ]


# LLM: _one_staged_checkpoint_findings checks existence and lightweight data quality for one checkpoint.
# 函数用途: 针对 JSON/脚本等阶段产物给出稳定错误码，供恢复包精确续接。
def _one_staged_checkpoint_findings(ref: str, task_workspace: Path) -> list[dict[str, object]]:
    path = _artifact_path({"preferred_path": ref}, task_workspace)
    if not path.exists():
        return [_staged_finding("STAGED_ARTIFACT_MISSING", ref, path, "Staged checkpoint does not exist.")]
    if path.suffix.lower() == ".json" and not _json_has_rows(path):
        return [_staged_finding("STAGED_JSON_NO_ROWS", ref, path, "Staged JSON checkpoint has no data rows.")]
    if path.is_file() and path.stat().st_size <= 0:
        return [_staged_finding("STAGED_ARTIFACT_EMPTY", ref, path, "Staged checkpoint is empty.")]
    return []


# LLM: _json_has_rows recognizes common structured data containers without natural-language parsing.
# 函数用途: 判断 JSON 里是否包含非空 list 数据，适配 top10/rows/projects/items 等常见机器字段。
def _json_has_rows(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return _contains_nonempty_list(value)


# LLM: _contains_nonempty_list recursively checks data shape, not prose content.
# 函数用途: 递归判断 JSON 对象/数组中是否有非空列表，避免把空数据 checkpoint 当成有效。
def _contains_nonempty_list(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return any(_contains_nonempty_list(item) for item in value.values())
    return False


# LLM: _staged_finding creates one machine-readable checkpoint issue.
# 函数用途: 统一生成 staging checkpoint finding，报告里同时保留 ref 和绝对位置。
def _staged_finding(code: str, ref: str, path: Path, message: str) -> dict[str, object]:
    return {
        "code": code,
        "severity": "hard",
        "stage_ref": ref,
        "location": str(path),
        "message": message,
    }


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
def _runtime_findings(task_workspace: Path) -> list[dict[str, object]]:
    return [_open_session_finding(session) for session in open_file_write_sessions(task_workspace, limit=20)]


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
