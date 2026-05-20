# LLM: Delivery closeout recovery helpers turn structured validation failures into generic next actions.
# 模块用途: 根据 machine contract、checkpoint 文件状态和 finding code 生成恢复动作，不针对某个真实任务写专项逻辑。

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts.error_taxonomy import error_contract
from ..contracts.staged_checkpoint_acceptance import (
    json_checkpoint_status,
    staged_json_evidence_findings,
)
from .main_agent_delivery_closeout_artifacts import (
    _artifact_path,
    _required_artifacts,
    _validation_contract,
)
from .main_agent_delivery_closeout_recovery_models import (
    CheckpointQualityActionRequest,
    RecoveryActionLedger,
    StagedEvidenceActionRequest,
    StagingActionContext,
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
    _append_failure_recovery_actions(report, ledger)
    ledger.actions.extend(_contract_recovery_actions(contract, workspace_root=workspace_root, seen=ledger.seen))
    if ledger.actions:
        return ledger.actions
    return [_generic_recovery_action("ACCEPTANCE_FAILED")]


# LLM: _append_failure_recovery_actions maps validator finding codes through the shared error taxonomy.
# 函数用途: 将验收器返回的结构化 code 转成 retryable/category/action/hint，不读取 message 文案。
def _append_failure_recovery_actions(report: dict[str, Any], ledger: RecoveryActionLedger) -> None:
    for finding in _failed_findings(report):
        recovery_contract = error_contract(_recovery_error_code(str(finding.get("code") or "")))
        if recovery_contract.code in ledger.seen:
            continue
        ledger.seen.add(recovery_contract.code)
        ledger.actions.append(_recovery_contract_action(recovery_contract))


# LLM: _failed_findings yields only structured findings from failed artifact reports.
# 函数用途: 将失败产物里的 finding 扁平化，跳过成功产物和非 dict 噪音。
def _failed_findings(report: dict[str, Any]):
    for item in report.get("artifacts", []):
        if item.get("ok"):
            continue
        yield from _artifact_findings(item)


# LLM: _artifact_findings extracts finding dicts from one artifact acceptance report.
# 函数用途: 防御性读取 acceptance_report.findings，保证坏结构不会打断 closeout。
def _artifact_findings(item: dict[str, Any]):
    findings = item.get("acceptance_report", {}).get("findings", [])
    yield from (finding for finding in findings if isinstance(finding, dict))


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
        _append_checkpoint_actions(context)
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
    source_ref = str(staging.get("source_json_ref") or "").strip()
    return StagingActionContext(
        ledger=ledger,
        staging=staging,
        artifact_exists=bool(artifact_path and artifact_path.exists()),
        builder_tool=str(staging.get("builder_tool") or "").strip(),
        source_ref=source_ref,
        output_ref=str(staging.get("workbook_ref") or artifact.get("preferred_path") or "").strip(),
        source_path=_artifact_path(source_ref, workspace_root) if source_ref else None,
        required_columns=_required_columns(validation_contract.get("required_columns")),
        source_shape_hint=_checkpoint_shape_hint(staging, source_ref),
        workspace_root=workspace_root,
    )


# LLM: _append_source_actions handles existing source checkpoint quality and evidence repair before builder actions.
# 函数用途: 如果 source_json_ref 已存在，先检查 JSON/列/证据质量；存在证据问题时暂缓 builder。
def _append_source_actions(context: StagingActionContext, validation_contract: dict[str, object]) -> bool:
    if context.source_path is None or not context.source_path.exists():
        return False
    _append_checkpoint_quality_action(
        CheckpointQualityActionRequest(
            context.ledger,
            context.source_ref,
            context.source_path,
            required_columns=context.required_columns,
            checkpoint_shape_hint=context.source_shape_hint,
        )
    )
    return _append_staged_evidence_actions(
        StagedEvidenceActionRequest(
            context.ledger,
            context.source_ref,
            context.workspace_root,
            validation_contract,
        )
    )


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
    return json_checkpoint_status(context.source_path, required_columns=context.required_columns).get("code") == "OK"


# LLM: _append_checkpoint_actions emits repair steps for declared checkpoint refs.
# 函数用途: 按 checkpoint_refs 检查缺失或坏 JSON 的阶段文件，缺第一个就提示先物化它。
def _append_checkpoint_actions(context: StagingActionContext) -> None:
    for ref_text in _checkpoint_refs(context.staging):
        checkpoint_path = _artifact_path(ref_text, context.workspace_root)
        if _handle_existing_checkpoint(context, ref_text, checkpoint_path):
            continue
        _append_missing_checkpoint_action(context, ref_text)
        break


# LLM: _checkpoint_refs normalizes staging checkpoint refs.
# 函数用途: 提取非空字符串 checkpoint 引用，供恢复动作按顺序检查。
def _checkpoint_refs(staging: dict[str, Any]) -> list[str]:
    refs = staging.get("checkpoint_refs")
    if not isinstance(refs, list):
        return []
    return [text for ref in refs if (text := str(ref or "").strip())]


# LLM: _handle_existing_checkpoint validates already-materialized JSON checkpoint refs.
# 函数用途: 已存在的 JSON checkpoint 进入质量检查；不存在的 checkpoint 留给缺失动作处理。
def _handle_existing_checkpoint(context: StagingActionContext, ref_text: str, checkpoint_path: Path | None) -> bool:
    if checkpoint_path is None:
        return True
    if not checkpoint_path.exists():
        return False
    if checkpoint_path.suffix.lower() == ".json":
        _append_checkpoint_quality_action(
            CheckpointQualityActionRequest(
                context.ledger,
                ref_text,
                checkpoint_path,
                required_columns=context.required_columns,
                checkpoint_shape_hint=_checkpoint_shape_hint(context.staging, ref_text),
            )
        )
    return True


# LLM: _append_missing_checkpoint_action reports the first missing staged checkpoint as a materialization task.
# 函数用途: 生成 materialize_checkpoint 恢复动作，并带上结构提示帮助下一轮写出最小有效骨架。
def _append_missing_checkpoint_action(context: StagingActionContext, ref_text: str) -> None:
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
            "recovery_hint": "先把缺失的阶段文件真实写出来，可以先写最小有效骨架，再继续补内容。",
            "checkpoint_shape_hint": _checkpoint_shape_hint(context.staging, ref_text),
        }
    )


# LLM: _append_checkpoint_quality_action promotes existing staged JSON quality issues into structured recovery actions.
# 函数用途: 当 checkpoint 文件已存在但 JSON 为空或损坏时，直接产出恢复动作，避免模型误以为可以继续 builder。
def _append_checkpoint_quality_action(request: CheckpointQualityActionRequest) -> None:
    status = json_checkpoint_status(request.checkpoint_path, required_columns=request.required_columns)
    action_code = str(status.get("code") or "")
    if action_code == "OK" or action_code in request.ledger.seen:
        return
    request.ledger.seen.add(action_code)
    action = _checkpoint_quality_action(request, status, error_contract(action_code))
    request.ledger.actions.append(action)


# LLM: _checkpoint_quality_action serializes checkpoint validation status into a recovery action.
# 函数用途: 将 parse_error、missing_columns、required_columns 等机器字段带入恢复动作。
def _checkpoint_quality_action(
    request: CheckpointQualityActionRequest,
    status: dict[str, str],
    contract,
) -> dict[str, object]:
    action = {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
        "checkpoint_ref": request.checkpoint_ref,
    }
    action.update(_checkpoint_optional_fields(request, status))
    return action


# LLM: _checkpoint_optional_fields keeps optional quality metadata compact and reusable.
# 函数用途: 只在字段真实存在时写入 required_columns、shape_hint、parse_error、missing_columns。
def _checkpoint_optional_fields(
    request: CheckpointQualityActionRequest,
    status: dict[str, str],
) -> dict[str, object]:
    fields: dict[str, object] = {}
    if request.required_columns:
        fields["required_columns"] = request.required_columns
    if request.checkpoint_shape_hint:
        fields["checkpoint_shape_hint"] = request.checkpoint_shape_hint
    for key in ("parse_error", "missing_columns"):
        if value := str(status.get(key) or ""):
            fields[key] = value
    return fields


# LLM: _checkpoint_shape_hint reads per-checkpoint structure hints so recovery actions can tell the model what shape to write.
# 函数用途: 从 staging_contract.checkpoint_shape_hints 中取出指定 checkpoint 的结构提示文本。
def _checkpoint_shape_hint(staging: dict[str, Any], checkpoint_ref: str) -> str:
    hints = staging.get("checkpoint_shape_hints")
    if not isinstance(hints, dict):
        return ""
    return str(hints.get(checkpoint_ref) or "").strip()


# LLM: _append_staged_evidence_actions converts staged evidence findings into structured recovery actions.
# 函数用途: 把 source_refs/claims 这类阶段证据问题写成 recovery_actions，供 closeout 和 repair guard 复用。
def _append_staged_evidence_actions(request: StagedEvidenceActionRequest) -> bool:
    evidence_contract = request.validation_contract.get("evidence_contract")
    if not isinstance(evidence_contract, dict):
        return False
    findings = staged_json_evidence_findings(request.checkpoint_ref, request.workspace_root, evidence_contract)
    for finding in findings:
        _append_one_evidence_action(request.ledger, request.checkpoint_ref, finding)
    return bool(findings)


# LLM: _append_one_evidence_action records one evidence finding as a de-duplicated recovery action.
# 函数用途: 将 evidence finding 的 code/field/claim_id 写入结构化恢复动作。
def _append_one_evidence_action(
    ledger: RecoveryActionLedger,
    checkpoint_ref: str,
    finding: dict[str, object],
) -> None:
    action_code = str(finding.get("code") or "")
    if not action_code or action_code in ledger.seen:
        return
    contract = error_contract(action_code)
    ledger.seen.add(action_code)
    action = _recovery_contract_action(contract)
    action["checkpoint_ref"] = checkpoint_ref
    if field := str(finding.get("field") or ""):
        action["field"] = field
    if claim_id := str(finding.get("claim_id") or ""):
        action["claim_id"] = claim_id
    ledger.actions.append(action)


# LLM: _required_columns normalizes contract-declared table columns without reading prompt prose.
# 函数用途: 从 validation_contract.required_columns 读取结构化列名，供阶段 JSON 和最终 workbook 共用同一列要求。
def _required_columns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [column for item in value if (column := str(item).strip())]


# LLM: _recovery_error_code groups validator-specific findings under generic recovery contracts.
# 函数用途: 把 HTML/XLSX/PDF 等专用 finding code 归并到通用错误类型，避免再长专项合同分支。
def _recovery_error_code(code: str) -> str:
    upper = str(code or "").upper()
    if upper in {"ARTIFACT_MISSING", "ARTIFACT_EMPTY"}:
        return "ARTIFACT_MISSING"
    if upper.startswith(("STAGED_", "PATH_", "SPREADSHEET_SOURCE_", "EVIDENCE_")):
        return upper
    if upper.startswith(("XLSX_", "CSV_", "JSON_", "PDF_", "HTML_")):
        return "ACCEPTANCE_FAILED"
    return "ACCEPTANCE_FAILED"
