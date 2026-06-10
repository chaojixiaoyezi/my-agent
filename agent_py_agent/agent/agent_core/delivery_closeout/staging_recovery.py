from __future__ import annotations

from pathlib import Path
from typing import Any

from ...contracts.recovery import RecoveryAction
from .artifacts import (
    _artifact_path,
    _required_artifacts,
    _validation_contract,
)
from .checkpoint_quality import (
    append_checkpoint_quality_action,
    checkpoint_writer_fields,
    required_sheets_min,
)
from .evidence import append_staged_evidence_actions
from .models import (
    CheckpointQualityActionRequest,
    RecoveryActionLedger,
    StagedEvidenceActionRequest,
    StagingActionContext,
)
from .staging import (
    StagingPrerequisiteRequest,
    staged_input_ready,
    staging_checkpoint_refs,
    staging_output_ref,
    staging_prerequisites_ready,
    staging_source_ref,
)


def append_contract_staging_recovery_actions(
    contract: dict[str, Any],
    *,
    workspace_root: Path,
    seen: set[str],
) -> list[dict[str, object]]:
    ledger = RecoveryActionLedger(actions=[], seen=seen)
    for artifact in _required_artifacts(contract):
        context = staging_action_context(artifact, workspace_root, ledger)
        if context is None:
            continue
        validation_contract = _validation_contract(artifact)
        evidence_repaired = _append_source_actions(context, validation_contract)
        if not evidence_repaired:
            _append_builder_ready_action(context)
        _append_checkpoint_actions(context, validation_contract)
    return ledger.actions


def staging_action_context(
    artifact: dict[str, Any],
    workspace_root: Path,
    ledger: RecoveryActionLedger,
) -> StagingActionContext | None:
    validation_contract = _validation_contract(artifact)
    staging = validation_contract.get("staging_contract")
    if not isinstance(staging, dict):
        return None
    artifact_path = _artifact_path(str(artifact.get("preferred_path") or artifact.get("path") or ""), workspace_root)
    source_ref = staging_source_ref(staging)
    return StagingActionContext(
        ledger=ledger,
        staging=staging,
        artifact_exists=bool(artifact_path and artifact_path.exists()),
        builder_tool=str(staging.get("builder_tool") or "").strip(),
        source_ref=source_ref,
        output_ref=staging_output_ref(staging, artifact),
        source_path=_artifact_path(source_ref, workspace_root) if source_ref else None,
        required_columns=_required_columns(validation_contract.get("required_columns")),
        required_sheets_min=required_sheets_min(validation_contract.get("required_sheets_min")),
        source_shape_hint=_checkpoint_shape_hint(staging, source_ref),
        workspace_root=workspace_root,
    )


def _append_source_actions(context: StagingActionContext, validation_contract: dict[str, object]) -> bool:
    evidence_repaired = _append_collection_source_evidence_action(context, validation_contract)
    if context.source_path is None or not context.source_path.exists():
        return evidence_repaired
    if context.source_path.suffix.lower() != ".json":
        return evidence_repaired
    append_checkpoint_quality_action(
        CheckpointQualityActionRequest(
            context.ledger,
            context.source_ref,
            context.source_path,
            required_columns=context.required_columns,
            required_sheets_min=context.required_sheets_min,
            checkpoint_shape_hint=context.source_shape_hint,
            validation_contract=validation_contract,
        )
    )
    return (
        append_staged_evidence_actions(
            StagedEvidenceActionRequest(
                context.ledger,
                context.source_ref,
                context.workspace_root,
                validation_contract,
            )
        )
        or evidence_repaired
    )


def _append_collection_source_evidence_action(
    context: StagingActionContext,
    validation_contract: dict[str, object],
) -> bool:
    source_ref = _collection_source_ref(validation_contract)
    if not source_ref or source_ref == context.source_ref:
        return False
    source_path = _artifact_path(source_ref, context.workspace_root)
    if source_path is None or not source_path.exists() or source_path.suffix.lower() != ".json":
        return False
    before = len(context.ledger.actions)
    append_checkpoint_quality_action(
        CheckpointQualityActionRequest(
            context.ledger,
            source_ref,
            source_path,
            required_columns=context.required_columns,
            required_sheets_min=context.required_sheets_min,
            checkpoint_shape_hint=_checkpoint_shape_hint(context.staging, source_ref),
            validation_contract=validation_contract,
        )
    )
    evidence_repaired = append_staged_evidence_actions(
        StagedEvidenceActionRequest(
            context.ledger,
            source_ref,
            context.workspace_root,
            validation_contract,
        )
    )
    return evidence_repaired or len(context.ledger.actions) > before


def _collection_source_ref(validation_contract: dict[str, object]) -> str:
    contract = validation_contract.get("collection_contract")
    if not isinstance(contract, dict):
        return ""
    return str(contract.get("source_json_ref") or "").strip()


def _append_builder_ready_action(context: StagingActionContext) -> None:
    if not _builder_ready(context):
        return
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
            "recovery_hint": "阶段数据已经落地；优先调用 builder tool 继续物化最终产物。",
        }
    )


def _builder_ready(context: StagingActionContext) -> bool:
    if not context.builder_tool or context.source_path is None or context.artifact_exists:
        return False
    if not context.source_path.exists():
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


def _append_checkpoint_actions(context: StagingActionContext, validation_contract: dict[str, object]) -> None:
    for ref_text in staging_checkpoint_refs(context.staging):
        if _is_builder_output_ref(context, ref_text):
            break
        checkpoint_path = _artifact_path(ref_text, context.workspace_root)
        if _handle_existing_checkpoint(context, ref_text, checkpoint_path, validation_contract):
            continue
        _append_missing_checkpoint_action(context, ref_text, validation_contract)
        break


def _is_builder_output_ref(context: StagingActionContext, ref_text: str) -> bool:
    return bool(context.builder_tool and context.output_ref and ref_text == context.output_ref)


def _handle_existing_checkpoint(
    context: StagingActionContext,
    ref_text: str,
    checkpoint_path: Path | None,
    validation_contract: dict[str, object],
) -> bool:
    if checkpoint_path is None:
        return True
    if not checkpoint_path.exists():
        return False
    if checkpoint_path.suffix.lower() == ".json":
        append_checkpoint_quality_action(
            CheckpointQualityActionRequest(
                context.ledger,
                ref_text,
                checkpoint_path,
                required_columns=context.required_columns,
                required_sheets_min=context.required_sheets_min,
                checkpoint_shape_hint=_checkpoint_shape_hint(context.staging, ref_text),
                validation_contract=validation_contract,
            )
        )
    return True


def _append_missing_checkpoint_action(
    context: StagingActionContext,
    ref_text: str,
    validation_contract: dict[str, object],
) -> None:
    action_code = "STAGING_CHECKPOINT_MISSING"
    if action_code in context.ledger.seen:
        return
    context.ledger.seen.add(action_code)
    context.ledger.actions.append(
        {
            "code": action_code,
            "category": "artifact",
            "retryable": True,
            "recommended_action": RecoveryAction.MATERIALIZE_CHECKPOINT.value,
            "checkpoint_ref": ref_text,
            "recovery_hint": missing_checkpoint_recovery_hint(ref_text, validation_contract),
            "checkpoint_shape_hint": _checkpoint_shape_hint(context.staging, ref_text),
            **checkpoint_materialization_fields(ref_text, validation_contract),
        }
    )


def _checkpoint_shape_hint(staging: dict[str, Any], checkpoint_ref: str) -> str:
    hints = staging.get("checkpoint_shape_hints")
    if not isinstance(hints, dict):
        return ""
    return str(hints.get(checkpoint_ref) or "").strip()


def _required_columns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [column for item in value if (column := str(item).strip())]


__all__ = ["append_contract_staging_recovery_actions", "staging_action_context"]


# ---- 原 source_checkpoint.py 并入 ----
def missing_checkpoint_recovery_hint(ref_text: str, validation_contract: dict[str, object]) -> str:
    if source_evidence_checkpoint_ref(ref_text, validation_contract):
        return (
            "缺失的是来源型 checkpoint；优先用采集/转换工具真实物化，"
            "必须带 source_refs、claims、completion_evidence 和行级 field_source_ids，不能写空骨架。"
        )
    return "先把缺失的阶段文件真实写出来，可以先写最小有效骨架，再继续补内容。"


def checkpoint_materialization_fields(ref_text: str, validation_contract: dict[str, object]) -> dict[str, object]:
    fields = checkpoint_writer_fields(ref_text)
    if not source_evidence_checkpoint_ref(ref_text, validation_contract):
        return fields
    collection = validation_contract.get("collection_contract")
    collection_contract = dict(collection) if isinstance(collection, dict) else {}
    return {
        **fields,
        "collection_contract": collection_contract,
        "checkpoint_materialization_mode": "source_evidence_first",
        "required_columns": _required_source_columns(collection_contract),
        "required_structured_fields": ["source_refs", "claims", "completion_evidence", "field_source_ids"],
        "requires_auditable_source_evidence": True,
        "write_tools": ["write_file"],
        "writer_tool": "write_file",
    }


def source_evidence_checkpoint_ref(ref_text: str, validation_contract: dict[str, object]) -> bool:
    collection = validation_contract.get("collection_contract")
    if not isinstance(collection, dict):
        return False
    source_ref = str(collection.get("source_json_ref") or "").strip()
    if not source_ref or ref_text != source_ref:
        return False
    evidence = validation_contract.get("evidence_contract")
    return bool(
        isinstance(evidence, dict)
        or collection.get("require_item_evidence")
        or collection.get("required_item_evidence_fields")
        or collection.get("require_completion_evidence")
    )


def _required_source_columns(collection_contract: dict[str, object]) -> list[str]:
    fields = collection_contract.get("required_item_fields")
    columns = [str(item).strip() for item in fields if str(item).strip()] if isinstance(fields, list) else []
    return list(dict.fromkeys(columns))


__all__ = [
    "checkpoint_materialization_fields",
    "missing_checkpoint_recovery_hint",
    "source_evidence_checkpoint_ref",
]
