# LLM: Live-LLM fake-tool contracts turn expensive real model trials into replayable facts.
# 模块用途: 校验真实模型加假工具测试是否记录 refs、验收结果和工具边界违规指标。

from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    positive_int,
    text,
    validation_report,
)

REQUIRED_TRIAL_REFS = ("prompt_ref", "response_ref", "tool_trace_ref", "contract_hash")


# LLM: validate_live_llm_fake_tool_trial checks recorded trial facts without calling the model.
# 函数用途: 用 refs/final_claim/verifier/metrics 校验真实 LLM + 假工具链路是否可回放、可验收。
def validate_live_llm_fake_tool_trial(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    refs = facts.get("refs") if isinstance(facts.get("refs"), dict) else {}
    metrics = facts.get("metrics") if isinstance(facts.get("metrics"), dict) else {}
    _validate_trial_shape(facts, refs, findings)
    _validate_completion_claim(facts, findings)
    _validate_metrics(metrics, findings)
    return validation_report(findings)


# LLM: _validate_trial_shape requires fake-tool mode and replayable artifact refs.
# 函数用途: 真实 LLM 测试必须声明 real_llm/tool_mode 并保存 prompt/response/trace/contract refs。
def _validate_trial_shape(
    facts: dict[str, Any],
    refs: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    missing = tuple(name for name in REQUIRED_TRIAL_REFS if not text(refs.get(name)))
    if missing:
        findings.append(finding("LIVE_LLM_TRIAL_REF_MISSING", {"refs": missing}))
    if facts.get("real_llm") is not True or text(facts.get("tool_mode")) != "fake":
        findings.append(finding("LIVE_LLM_TRIAL_MODE_INVALID"))


# LLM: _validate_completion_claim rejects model success when verifier facts disagree.
# 函数用途: 模型说 succeeded 但 verifier.ok=false 时，记录假完成 finding。
def _validate_completion_claim(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    claim = facts.get("final_claim") if isinstance(facts.get("final_claim"), dict) else {}
    verifier = facts.get("verifier") if isinstance(facts.get("verifier"), dict) else {}
    if text(claim.get("status")).lower() == "succeeded" and verifier.get("ok") is not True:
        findings.append(finding("LIVE_LLM_FAKE_COMPLETION_REJECTED"))


# LLM: _validate_metrics rejects structured tool boundary violations seen in a real-model trial.
# 函数用途: unknown tool、schema error、工具失败假成功、dry-run 当真执行都进入回归指标。
def _validate_metrics(metrics: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if positive_int(metrics.get("unknown_tool_count")):
        findings.append(finding("LIVE_LLM_UNKNOWN_TOOL"))
    if positive_int(metrics.get("schema_error_count")):
        findings.append(finding("LIVE_LLM_TOOL_SCHEMA_ERROR"))
    if metrics.get("tool_failure_claimed_success") is True:
        findings.append(finding("LIVE_LLM_TOOL_FAILURE_CLAIMED_SUCCESS"))
    if metrics.get("dry_run_claimed_real") is True:
        findings.append(finding("LIVE_LLM_DRY_RUN_CLAIMED_REAL"))


__all__ = ["validate_live_llm_fake_tool_trial"]
