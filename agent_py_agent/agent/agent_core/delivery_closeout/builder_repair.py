
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contracts.recovery_actions import RecoveryAction
from .artifacts import (
    _artifact_path,
    _required_artifacts,
)
from .recovery_models import RecoveryActionLedger, StagingActionContext
from .staging import (
    StagingPrerequisiteRequest,
    staged_input_ready,
    staging_checkpoint_refs,
    staging_prerequisites_ready,
)

StagingContextFactory = Callable[[dict[str, Any], Path, RecoveryActionLedger], StagingActionContext | None]


@dataclass(frozen=True)
class BuilderRepairRequest:
    report: dict[str, Any]
    contract: dict[str, Any]
    workspace_root: Path
    ledger: RecoveryActionLedger
    staging_context: StagingContextFactory


def append_failed_builder_output_actions(request: BuilderRepairRequest) -> None:
    for item in _failed_report_artifacts(request.report):
        artifact = _matching_contract_artifact(item, request.contract, request.workspace_root)
        context = request.staging_context(artifact, request.workspace_root, request.ledger) if artifact is not None else None
        if isinstance(context, StagingActionContext) and _failed_builder_output_ready(context):
            _append_builder_repair_action(context)


def _failed_report_artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in report.get("artifacts", []) if isinstance(item, dict) and not item.get("ok")]


def _matching_contract_artifact(
    report_item: dict[str, Any],
    contract: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any] | None:
    report_id = str(report_item.get("artifact_id") or "").strip()
    report_path = _resolved_report_path(report_item.get("path"))
    for artifact in _required_artifacts(contract):
        if _artifact_matches_report(artifact, report_id, report_path, workspace_root):
            return artifact
    return None


def _artifact_matches_report(
    artifact: dict[str, Any],
    report_id: str,
    report_path: Path | None,
    workspace_root: Path,
) -> bool:
    artifact_id = str(artifact.get("artifact_id") or "").strip()
    if report_id and artifact_id == report_id:
        return True
    expected = _artifact_path(str(artifact.get("preferred_path") or artifact.get("path") or ""), workspace_root)
    return bool(expected is not None and report_path is not None and expected == report_path)


def _resolved_report_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except OSError:
        return None


def _failed_builder_output_ready(context: StagingActionContext) -> bool:
    if not context.builder_tool or context.source_path is None or not context.source_path.exists():
        return False
    if _source_repair_pending(context):
        return False
    return staged_input_ready(
        context.source_ref,
        context.workspace_root,
        required_columns=context.required_columns,
        required_sheets_min=context.required_sheets_min,
    ) and staging_prerequisites_ready(
        StagingPrerequisiteRequest(
            context.staging,
            context.workspace_root,
            context.output_ref,
            context.source_ref,
            context.required_columns,
            context.required_sheets_min,
        )
    )


def _source_repair_pending(context: StagingActionContext) -> bool:
    source_actions = {
        "materialize_checkpoint",
        "repair_artifact_against_findings",
        "repair_evidence_refs",
        "repair_structured_checkpoint_json",
        "write_non_empty_structured_rows",
    }
    target_refs = _pre_builder_checkpoint_refs(context)
    return any(_is_source_repair_action(action, target_refs, source_actions) for action in context.ledger.actions)


def _is_source_repair_action(action: dict[str, object], target_refs: set[str], source_actions: set[str]) -> bool:
    recommended = str(action.get("recommended_action") or "")
    if recommended not in source_actions:
        return False
    checkpoint_ref = str(action.get("checkpoint_ref") or "")
    if checkpoint_ref in target_refs:
        return True
    return bool(_repair_targets_hit_source(action, target_refs))


def _repair_targets_hit_source(action: dict[str, object], target_refs: set[str]) -> bool:
    targets = action.get("repair_targets")
    if not isinstance(targets, list):
        return False
    normalized = {_normalize_ref(value) for value in target_refs}
    return any(_target_matches_ref(_normalize_ref(value), normalized) for value in targets)


def _target_matches_ref(target: str, refs: set[str]) -> bool:
    return any(target == ref or target.endswith(f"/{ref}") for ref in refs)


def _normalize_ref(value: object) -> str:
    return str(value or "").replace("\\", "/").strip()


def _pre_builder_checkpoint_refs(context: StagingActionContext) -> set[str]:
    refs: set[str] = {context.source_ref}
    for ref_text in staging_checkpoint_refs(context.staging):
        if ref_text == context.output_ref:
            break
        refs.add(ref_text)
    return refs


def _append_builder_repair_action(context: StagingActionContext) -> None:
    action_code = "STAGING_BUILDER_READY"
    if action_code in context.ledger.seen:
        return
    context.ledger.seen.add(action_code)
    context.ledger.actions.append(
        {
            "code": action_code,
            "category": "artifact",
            "retryable": True,
            "recommended_action": RecoveryAction.WRITE_TARGET_ARTIFACT.value,
            "builder_tool": context.builder_tool,
            "source_ref": context.source_ref,
            "output_ref": context.output_ref,
            "recovery_hint": "阶段数据已经落地；优先调用 builder tool 重新物化未通过验收的最终产物。",
        }
    )


__all__ = ["BuilderRepairRequest", "append_failed_builder_output_actions"]
