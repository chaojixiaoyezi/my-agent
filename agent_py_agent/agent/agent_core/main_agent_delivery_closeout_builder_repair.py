# LLM: Builder repair actions regenerate failed final artifacts from ready staged sources.
# 模块用途: 当 workbook/pdf 等最终产物存在但验收失败时，基于结构化 staging_contract 生成 builder tool 动作。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .main_agent_delivery_closeout_artifacts import (
    _artifact_path,
    _required_artifacts,
)
from .main_agent_delivery_closeout_recovery_models import RecoveryActionLedger, StagingActionContext
from .main_agent_delivery_closeout_staging import (
    StagingPrerequisiteRequest,
    staged_input_ready,
    staging_checkpoint_refs,
    staging_prerequisites_ready,
)

StagingContextFactory = Callable[[dict[str, Any], Path, RecoveryActionLedger], StagingActionContext | None]


# LLM: BuilderRepairRequest bundles the closeout report and staging context factory.
# 类用途: 将 builder repair 输入作为一个结构化请求传递，避免恢复入口参数继续膨胀。
@dataclass(frozen=True)
class BuilderRepairRequest:
    report: dict[str, Any]
    contract: dict[str, Any]
    workspace_root: Path
    ledger: RecoveryActionLedger
    staging_context: StagingContextFactory


# LLM: append_failed_builder_output_actions regenerates failed builder artifacts from ready staged sources.
# 函数用途: 最终 workbook/pdf 已存在但验收失败时，若 source checkpoint 已就绪，优先给出 builder_tool 调用合同。
def append_failed_builder_output_actions(request: BuilderRepairRequest) -> None:
    for item in _failed_report_artifacts(request.report):
        artifact = _matching_contract_artifact(item, request.contract, request.workspace_root)
        context = request.staging_context(artifact, request.workspace_root, request.ledger) if artifact is not None else None
        if isinstance(context, StagingActionContext) and _failed_builder_output_ready(context):
            _append_builder_repair_action(context)


# LLM: _failed_report_artifacts yields failed artifact records from a closeout report.
# 函数用途: 忽略已通过或非 dict 的 artifact 项。
def _failed_report_artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in report.get("artifacts", []) if isinstance(item, dict) and not item.get("ok")]


# LLM: _matching_contract_artifact maps a closeout artifact report back to its machine contract item.
# 函数用途: 用 artifact_id 或规范化路径匹配合同产物，不读取任务说明文字。
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


# LLM: _artifact_matches_report checks artifact_id first, then resolved path.
# 函数用途: closeout 和 contract 都有机器 id 时优先用 id；没有 id 才用路径。
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


# LLM: _resolved_report_path normalizes persisted closeout paths for artifact matching.
# 函数用途: 将 closeout 里的 artifact path 转成 resolved Path，坏路径返回 None。
def _resolved_report_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except OSError:
        return None


# LLM: _failed_builder_output_ready checks source readiness while allowing the output artifact to already exist.
# 函数用途: 与 missing-output builder ready 相似，但用于“已存在但失败”的 builder output。
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


# LLM: _source_repair_pending prevents rebuilding from a source checkpoint that already has repair actions.
# 函数用途: 如果同一个 source_ref 仍需补 JSON/证据/行数据，就先修 source，不提前生成最终产物。
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


# LLM: _is_source_repair_action checks whether one recovery action targets the staged source.
# 函数用途: 只按 recommended_action 和 checkpoint_ref 机器字段判断是否需要先修 source。
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


# LLM: _pre_builder_checkpoint_refs returns every checkpoint that must be healthy before the builder output.
# 函数用途: 防止 source_index.json 等前置证据文件待修时，仍然重建 markdown/pdf/xlsx 终产物。
def _pre_builder_checkpoint_refs(context: StagingActionContext) -> set[str]:
    refs: set[str] = {context.source_ref}
    for ref_text in staging_checkpoint_refs(context.staging):
        if ref_text == context.output_ref:
            break
        refs.add(ref_text)
    return refs


# LLM: _append_builder_repair_action emits the executable builder contract used for missing outputs.
# 函数用途: 对失败的 builder output 写入 builder_tool/source_ref/output_ref，供 repair guard 生成 required_tool_calls。
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
            "recommended_action": "invoke_builder_tool",
            "builder_tool": context.builder_tool,
            "source_ref": context.source_ref,
            "output_ref": context.output_ref,
            "recovery_hint": "阶段数据已经落地；优先调用 builder tool 重新物化未通过验收的最终产物。",
        }
    )


__all__ = ["BuilderRepairRequest", "append_failed_builder_output_actions"]
