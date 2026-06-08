
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
    validation_workspace_root_for_item,
)
from .artifact_candidate_paths import report_with_candidate_paths
from .contract_validation_recovery import recovery_for_findings
from .staged_checkpoint import staged_checkpoint_findings


@dataclass(frozen=True)
class TaskRunAcceptanceRequest:
    expected_artifacts_path: Path
    task_workspace: Path
    report_path: Path


@dataclass(frozen=True)
class TaskRunArtifactAcceptance:
    artifact_id: str
    path: str
    validator: str
    ok: bool
    report: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "validator": self.validator,
            "ok": self.ok,
            "report": dict(self.report),
        }


@dataclass(frozen=True)
class TaskRunAcceptanceReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    artifacts: list[TaskRunArtifactAcceptance] = field(default_factory=list)
    runtime_findings: list[dict[str, object]] = field(default_factory=list)
    recovery: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload = {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "runtime_findings": [dict(finding) for finding in self.runtime_findings],
        }
        recovery = self.recovery or recovery_for_findings(
            "task_run_acceptance",
            _acceptance_recovery_findings(self.artifacts, self.runtime_findings),
        )
        if recovery is not None:
            payload["recovery"] = recovery
        return payload


def validate_task_artifacts(request: TaskRunAcceptanceRequest) -> TaskRunAcceptanceReport:
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
    report = TaskRunAcceptanceReport(
        ok=all(item.ok for item in artifacts) and not runtime_findings,
        summary=_summary(artifacts, runtime_findings),
        report_ref=str(request.report_path),
        artifacts=artifacts,
        runtime_findings=runtime_findings,
        recovery=recovery_for_findings("task_run_acceptance", _acceptance_recovery_findings(artifacts, runtime_findings)),
    )
    _write_report(request.report_path, report.to_dict())
    return report


def _expected_artifacts(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [_missing_contract_item(path)]
    items = payload.get("artifacts") if isinstance(payload, dict) else None
    return [dict(item) for item in items] if isinstance(items, list) else []


def _validate_artifact_item(
    item: dict[str, object],
    task_workspace: Path,
) -> TaskRunArtifactAcceptance:
    artifact_id = str(item.get("artifact_id") or "")
    path = _artifact_path(item, task_workspace)
    validator = _validator_name(item)
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=validation_workspace_root_for_item(item, path, task_workspace),
            validation_contract=_validation_contract(item),
        )
    )
    report = report_with_candidate_paths(
        report,
        path,
        task_workspace,
        validation_contract=_validation_contract(item),
    ).to_dict()
    return TaskRunArtifactAcceptance(
        artifact_id=artifact_id,
        path=str(path),
        validator=validator,
        ok=bool(report.get("ok")),
        report=report,
    )


def _acceptance_recovery_findings(
    artifacts: list[TaskRunArtifactAcceptance],
    runtime_findings: list[dict[str, object]],
) -> tuple[dict[str, object], ...]:
    findings = [dict(item) for item in runtime_findings]
    findings.extend(
        {
            "code": "ARTIFACT_ACCEPTANCE_FAILED",
            "artifact_id": item.artifact_id,
            "path": item.path,
            "validator": item.validator,
        }
        for item in artifacts
        if not item.ok
    )
    return tuple(findings)


def _artifact_path(item: dict[str, object], task_workspace: Path) -> Path:
    raw = str(item.get("preferred_path") or "")
    candidate = Path(raw)
    return candidate.resolve(strict=False) if candidate.is_absolute() else (task_workspace / candidate).resolve(strict=False)


def _staged_checkpoint_findings(
    items: list[dict[str, object]],
    task_workspace: Path,
) -> list[dict[str, object]]:
    return staged_checkpoint_findings(items, task_workspace)


def _validator_name(item: dict[str, object]) -> str:
    contract = item.get("validation_contract")
    if not isinstance(contract, dict):
        return "artifact_acceptance"
    return str(contract.get("validator") or "artifact_acceptance")


def _validation_contract(item: dict[str, object]) -> dict[str, object]:
    contract = item.get("validation_contract")
    return dict(contract) if isinstance(contract, dict) else {}


def _runtime_findings(
    task_workspace: Path,
    artifacts: list[TaskRunArtifactAcceptance],
) -> list[dict[str, object]]:
    return []


def _summary(
    artifacts: list[TaskRunArtifactAcceptance],
    runtime_findings: list[dict[str, object]],
) -> dict[str, int]:
    failed = sum(not item.ok for item in artifacts)
    return {
        "total": len(artifacts) + len(runtime_findings),
        "passed": len(artifacts) - failed,
        "failed": failed + len(runtime_findings),
    }


def _missing_contract_item(path: Path) -> dict[str, object]:
    return {
        "artifact_id": "expected_artifacts_contract",
        "preferred_path": str(path),
        "validation_contract": {"validator": "artifact_acceptance"},
        "required": True,
    }


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


__all__ = [
    "TaskRunAcceptanceReport",
    "TaskRunAcceptanceRequest",
    "TaskRunArtifactAcceptance",
    "validate_task_artifacts",
]
