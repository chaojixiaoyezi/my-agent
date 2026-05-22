# LLM: Delivery closeout recovery helpers turn structured validation failures into generic next actions.
# 模块用途: 根据 machine contract、checkpoint 文件状态和 finding code 生成恢复动作，不针对某个真实任务写专项逻辑。

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts.error_taxonomy import error_contract
from .main_agent_delivery_closeout_artifact_repair import (
    append_artifact_finding_repair_actions,
    append_collection_value_repair_actions,
    failed_findings,
)
from .main_agent_delivery_closeout_artifacts import (
    _artifact_path,
    _required_artifacts,
    _validation_contract,
)
from .main_agent_delivery_closeout_builder_repair import (
    BuilderRepairRequest,
    append_failed_builder_output_actions,
)
from .main_agent_delivery_closeout_checkpoint_quality import (
    append_checkpoint_quality_action,
    required_sheets_min,
)
from .main_agent_delivery_closeout_evidence import append_staged_evidence_actions
from .main_agent_delivery_closeout_recovery_codes import recovery_error_code
from .main_agent_delivery_closeout_recovery_models import (
    CheckpointQualityActionRequest,
    RecoveryActionLedger,
    StagedEvidenceActionRequest,
    StagingActionContext,
)
from .main_agent_delivery_closeout_source_checkpoint import (
    checkpoint_materialization_fields,
    missing_checkpoint_recovery_hint,
)
from .main_agent_delivery_closeout_staging import (
    StagingPrerequisiteRequest,
    staged_input_ready,
    staging_checkpoint_refs,
    staging_output_ref,
    staging_prerequisites_ready,
    staging_source_ref,
)


# LLM: _recovery_actions lifts failed finding codes into generic next-step hints instead of task-specific prose.
# 函数用途: 给交付失败补充通用恢复动作，让模型和父级都能结构化知道下一步该做什么。
def _recovery_actions(
    report: dict[str, Any],
    *,
    contract: dict[str, Any],
    workspace_root: Path,
) -> list[dict[str, object]]:
    ledger = RecoveryActionLedger(actions=[], seen=set())
    ledger.actions.extend(_contract_recovery_actions(contract, workspace_root=workspace_root, seen=ledger.seen))
    _append_failure_recovery_actions(report, ledger, contract=contract, workspace_root=workspace_root)
    if ledger.actions:
        return ledger.actions
    return [_generic_recovery_action("ACCEPTANCE_FAILED")]


# LLM: _append_failure_recovery_actions maps validator finding codes through the shared error taxonomy.
# 函数用途: 将验收器返回的结构化 code 转成 retryable/category/action/hint，不读取 message 文案。
def _append_failure_recovery_actions(
    report: dict[str, Any],
    ledger: RecoveryActionLedger,
    *,
    contract: dict[str, Any],
    workspace_root: Path,
) -> None:
    append_collection_value_repair_actions(report, contract, ledger)
    append_artifact_finding_repair_actions(report, ledger)
    append_failed_builder_output_actions(
        BuilderRepairRequest(
            report=report,
            contract=contract,
            workspace_root=workspace_root,
            ledger=ledger,
            staging_context=_staging_action_context,
        )
    )
    for finding in failed_findings(report):
        recovery_contract = error_contract(recovery_error_code(str(finding.get("code") or "")))
        if recovery_contract.code in ledger.seen:
            continue
        ledger.seen.add(recovery_contract.code)
        ledger.actions.append(_recovery_contract_action(recovery_contract))


# LLM: _generic_recovery_action creates a stable fallback action from the shared taxonomy.
# 函数用途: 当没有更具体结构化线索时，返回通用 ACCEPTANCE_FAILED 恢复动作。
def _generic_recovery_action(code: str) -> dict[str, object]:
    return _recovery_contract_action(error_contract(code))


# LLM: _recovery_contract_action serializes one error taxonomy contract into closeout report JSON.
# 函数用途: 保持所有恢复动作的 code/category/retryable/recommended_action/recovery_hint 字段一致。
def _recovery_contract_action(contract) -> dict[str, object]:
    return {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
    }


# LLM: _contract_recovery_actions derives next steps from machine contracts and workspace facts only.
# 函数用途: 当阶段文件、builder tool、checkpoint refs 已在结构化合同里定义时，生成对应恢复动作。
def _contract_recovery_actions(
    contract: dict[str, Any],
    *,
    workspace_root: Path,
    seen: set[str],
) -> list[dict[str, object]]:
    ledger = RecoveryActionLedger(actions=[], seen=seen)
    for artifact in _required_artifacts(contract):
        context = _staging_action_context(artifact, workspace_root, ledger)
        if context is None:
            continue
        evidence_repaired = _append_source_actions(context, _validation_contract(artifact))
        if not evidence_repaired:
            _append_builder_ready_action(context)
        _append_checkpoint_actions(context, _validation_contract(artifact))
    return ledger.actions


# LLM: _staging_action_context normalizes one artifact's staging contract into resolved facts.
# 函数用途: 从 validation_contract.staging_contract 读取 source/builder/checkpoint 字段，并解析对应路径。
def _staging_action_context(
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


# LLM: _append_source_actions handles existing source checkpoint quality and evidence repair before builder actions.
# 函数用途: 如果 source_json_ref 已存在，先检查 JSON/列/证据质量；存在证据问题时暂缓 builder。
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


# LLM: Collection source evidence can live in a JSON checkpoint that is separate from the builder source.
# 函数用途: 支持 source_index.json -> markdown -> pdf 这类链路先修证据 JSON，再进入 builder。
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
    return append_staged_evidence_actions(
        StagedEvidenceActionRequest(
            context.ledger,
            source_ref,
            context.workspace_root,
            validation_contract,
        )
    )


# LLM: _collection_source_ref reads the source JSON ref declared by collection contracts.
# 函数用途: 从结构化 collection_contract 中取证据源 checkpoint，不从报告文字猜路径。
def _collection_source_ref(validation_contract: dict[str, object]) -> str:
    contract = validation_contract.get("collection_contract")
    if not isinstance(contract, dict):
        return ""
    return str(contract.get("source_json_ref") or "").strip()


# LLM: _append_builder_ready_action emits builder readiness only after source JSON is structurally valid.
# 函数用途: 当 builder/source/最终产物状态满足条件时，生成 invoke_builder_tool 恢复动作。
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
            "recommended_action": "invoke_builder_tool",
            "builder_tool": context.builder_tool,
            "source_ref": context.source_ref,
            "output_ref": context.output_ref,
            "recovery_hint": "阶段数据已经落地；优先调用 builder tool 继续物化最终产物。",
        }
    )


# LLM: _builder_ready keeps staged builder gating in one structural predicate.
# 函数用途: 只在 source JSON 存在、最终产物缺失且 checkpoint 状态 OK 时允许 builder ready。
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


# LLM: _append_checkpoint_actions emits repair steps for declared checkpoint refs.
# 函数用途: 按 checkpoint_refs 检查缺失或坏 JSON 的阶段文件，缺第一个就提示先物化它。
def _append_checkpoint_actions(context: StagingActionContext, validation_contract: dict[str, object]) -> None:
    for ref_text in staging_checkpoint_refs(context.staging):
        if _is_builder_output_ref(context, ref_text):
            break
        checkpoint_path = _artifact_path(ref_text, context.workspace_root)
        if _handle_existing_checkpoint(context, ref_text, checkpoint_path, validation_contract):
            continue
        _append_missing_checkpoint_action(context, ref_text, validation_contract)
        break


# LLM: Builder outputs are materialized by their declared builder tool, not by generic checkpoint writers.
# 函数用途: 避免 workbook/pdf 这类最终构建产物在 source 未就绪时生成误导性的 materialize_checkpoint 动作。
def _is_builder_output_ref(context: StagingActionContext, ref_text: str) -> bool:
    return bool(context.builder_tool and context.output_ref and ref_text == context.output_ref)


# LLM: _handle_existing_checkpoint validates already-materialized JSON checkpoint refs.
# 函数用途: 已存在的 JSON checkpoint 进入质量检查；不存在的 checkpoint 留给缺失动作处理。
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


# LLM: _append_missing_checkpoint_action reports the first missing staged checkpoint as a materialization task.
# 函数用途: 生成 materialize_checkpoint 恢复动作，并带上结构提示帮助下一轮写出最小有效骨架。
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
            "recommended_action": "materialize_checkpoint",
            "checkpoint_ref": ref_text,
            "recovery_hint": missing_checkpoint_recovery_hint(ref_text, validation_contract),
            "checkpoint_shape_hint": _checkpoint_shape_hint(context.staging, ref_text),
            **checkpoint_materialization_fields(ref_text, validation_contract),
        }
    )


# LLM: _checkpoint_shape_hint reads per-checkpoint structure hints so recovery actions can tell the model what shape to write.
# 函数用途: 从 staging_contract.checkpoint_shape_hints 中取出指定 checkpoint 的结构提示文本。
def _checkpoint_shape_hint(staging: dict[str, Any], checkpoint_ref: str) -> str:
    hints = staging.get("checkpoint_shape_hints")
    if not isinstance(hints, dict):
        return ""
    return str(hints.get(checkpoint_ref) or "").strip()


# LLM: _required_columns normalizes contract-declared table columns without reading prompt prose.
# 函数用途: 从 validation_contract.required_columns 读取结构化列名，供阶段 JSON 和最终 workbook 共用同一列要求。
def _required_columns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [column for item in value if (column := str(item).strip())]
