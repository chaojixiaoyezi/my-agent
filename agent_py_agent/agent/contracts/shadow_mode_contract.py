# LLM: Shadow-mode contracts record agent recommendations, dry-run evidence, and human comparison without real side effects.
# 模块用途: 校验影子模式运行的风险评分、证据链、建议动作、dry-run 结果、人工复核和真实执行隔离。

from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    string_tuple,
    text,
    validation_report,
)

SIDE_EFFECTS = {"mutating", "dangerous"}
REVIEW_ONLY_MODES = {"", "recommend", "draft", "dry_run"}


# LLM: validate_shadow_mode_run checks structured shadow-run facts before TaskTree or real execution.
# 函数用途: 从 risk/evidence_refs/recommended_actions/dry_run_results/executed_actions/human_review 校验影子模式。
def validate_shadow_mode_run(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_mode(facts, findings)
    _validate_risk(facts, findings)
    _validate_evidence(facts, findings)
    _validate_recommendations(facts, findings)
    _validate_no_real_execution(facts, findings)
    _validate_human_review(facts, findings)
    return validation_report(findings)


# LLM: _validate_mode keeps the shadow contract from being reused for real execution.
# 函数用途: mode 必须显式为 shadow，避免真实执行结果混入影子账本。
def _validate_mode(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(facts.get("mode")) != "shadow":
        findings.append(finding("SHADOW_MODE_INVALID"))


# LLM: _validate_risk requires bounded machine-readable risk scoring.
# 函数用途: 风险分必须是 0 到 100 的数字，不能只写自然语言风险描述。
def _validate_risk(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    risk = facts.get("risk") if isinstance(facts.get("risk"), dict) else {}
    score = _number(risk.get("score"))
    if score is None or score < 0 or score > 100:
        findings.append(finding("SHADOW_RISK_SCORE_MISSING"))


# LLM: _validate_evidence requires every evidence item to point at a structured source.
# 函数用途: 证据必须有 source_type 和 source_ref，不能只靠报告正文声明。
def _validate_evidence(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    evidence = dict_items(facts.get("evidence_refs"))
    if not evidence:
        findings.append(finding("SHADOW_EVIDENCE_SOURCE_MISSING"))
        return
    for item in evidence:
        if text(item.get("source_type")) and text(item.get("source_ref")):
            continue
        findings.append(finding("SHADOW_EVIDENCE_SOURCE_MISSING", {"evidence_id": text(item.get("evidence_id"))}))
        return


# LLM: _validate_recommendations ensures side-effect recommendations stay reviewable and backed by dry-run facts.
# 函数用途: 建议动作只能是 recommend/draft/dry_run；副作用动作需要 operator review ref，高危动作还要 dry-run 和审批草稿。
def _validate_recommendations(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    dry_run_ids = _successful_dry_run_action_ids(facts)
    for action in dict_items(facts.get("recommended_actions")):
        _validate_action_mode(action, findings)
        _validate_operator_review(action, findings)
        if text(action.get("effect")) == "dangerous":
            _validate_dangerous_action(action, dry_run_ids, findings)


# LLM: _validate_action_mode blocks recommendations that ask for real execution in shadow mode.
# 函数用途: recommended_actions[].mode 只能是 recommend/draft/dry_run。
def _validate_action_mode(action: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(action.get("mode")) not in REVIEW_ONLY_MODES:
        findings.append(finding("SHADOW_ACTION_NOT_REVIEW_ONLY", {"action_id": text(action.get("action_id"))}))


# LLM: _validate_operator_review requires side-effect recommendations to be inspectable by a person.
# 函数用途: mutating/dangerous 动作必须有 operator_review_ref，便于人工比较和审计。
def _validate_operator_review(action: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(action.get("effect")) in SIDE_EFFECTS and not text(action.get("operator_review_ref")):
        findings.append(finding("SHADOW_OPERATOR_REVIEW_REF_MISSING", {"action_id": text(action.get("action_id"))}))


# LLM: _validate_dangerous_action requires dangerous recommendations to have dry-run and approval-draft refs.
# 函数用途: 高危建议必须有匹配 dry-run 成功结果和审批草稿，不能直接走真实处置。
def _validate_dangerous_action(
    action: dict[str, Any],
    dry_run_ids: set[str],
    findings: list[dict[str, object]],
) -> None:
    action_id = text(action.get("action_id"))
    if action_id not in dry_run_ids:
        findings.append(finding("SHADOW_DANGEROUS_DRY_RUN_MISSING", {"action_id": action_id}))
    if not text(action.get("approval_draft_ref")):
        findings.append(finding("SHADOW_APPROVAL_DRAFT_MISSING", {"action_id": action_id}))


# LLM: _validate_no_real_execution rejects any applied action while still in shadow mode.
# 函数用途: executed_actions 中出现任何记录都视为真实副作用越界。
def _validate_no_real_execution(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if dict_items(facts.get("executed_actions")):
        findings.append(finding("SHADOW_REAL_ACTION_EXECUTED"))


# LLM: _validate_human_review stores the comparison record that makes shadow mode useful.
# 函数用途: 要求人工复核有 review_id、review_ref、decision、agreement；不一致时必须有结构化原因。
def _validate_human_review(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    review = facts.get("human_review") if isinstance(facts.get("human_review"), dict) else {}
    if not review:
        findings.append(finding("SHADOW_HUMAN_REVIEW_MISSING"))
        return
    if not (text(review.get("review_id")) and text(review.get("review_ref")) and text(review.get("decision"))):
        findings.append(finding("SHADOW_HUMAN_REVIEW_INCOMPLETE"))
    if review.get("agreement") is False and not _review_disagreement_reasons(review):
        findings.append(finding("SHADOW_REVIEW_DISAGREEMENT_REASON_MISSING"))


# LLM: _successful_dry_run_action_ids indexes dry-run results by action id.
# 函数用途: 返回 mode=dry_run、ok=true 且有 result_ref 的 action_id 集合。
def _successful_dry_run_action_ids(facts: dict[str, Any]) -> set[str]:
    return {
        text(item.get("action_id"))
        for item in dict_items(facts.get("dry_run_results"))
        if text(item.get("mode")) == "dry_run" and item.get("ok") is True and text(item.get("result_ref"))
    }


# LLM: _review_disagreement_reasons checks structured mismatch and evidence reason arrays.
# 函数用途: 人工不同意时必须至少给 mismatch_reason_codes 或 missing_evidence_codes。
def _review_disagreement_reasons(review: dict[str, Any]) -> tuple[str, ...]:
    return string_tuple(review.get("mismatch_reason_codes")) + string_tuple(review.get("missing_evidence_codes"))


# LLM: _number reads explicit numeric facts without parsing prose.
# 函数用途: 将 int/float 或数字字符串转为 float，缺失和非法返回 None。
def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["validate_shadow_mode_run"]
