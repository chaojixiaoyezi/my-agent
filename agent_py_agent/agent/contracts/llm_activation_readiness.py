# LLM: LLM activation readiness composes structured gates before live model calls are allowed.
# 模块用途: 将模型适配、上下文、工具副作用、canary、trace、超时预算和总门禁串成 refs-first 报告。

from __future__ import annotations

import json
from pathlib import Path

from .llm_activation_readiness_fixtures import (
    build_small_llm_canary_gate,
    model_adapter_facts,
    prompt_context_facts,
    side_effect_events,
    trace_capture_manifest,
    trace_events,
    trace_manifest_issues,
)
from .llm_activation_readiness_models import (
    LLMActivationReadinessPhase,
    LLMActivationReadinessReport,
    LLMActivationReadinessRequest,
    readiness_phase,
    readiness_summary,
)
from .llm_activation_timeout_budget import build_model_timeout_budget, timeout_budget_issues
from .offline_model_adapter_contract import validate_model_adapter_facts
from .offline_prompt_context_contract import validate_prompt_context
from .offline_side_effect_contract import validate_side_effects
from .pre_real_task_validation import PreRealTaskValidationReport
from .run_trace_contract import validate_run_trace_events
from .small_real_acceptance_gate import validate_small_real_acceptance_gate


# LLM: run_llm_activation_readiness is the public seven-step pre-live-model gate.
# 函数用途: 顺序执行 LLM 上场前 7 个结构化验收步骤，并写出 refs-first 总报告。
def run_llm_activation_readiness(
    request: LLMActivationReadinessRequest,
) -> LLMActivationReadinessReport:
    workspace = Path(request.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    phases = (
        _validate_model_adapter_phase(workspace),
        _validate_prompt_context_phase(workspace),
        _validate_side_effect_phase(workspace),
        _validate_canary_phase(workspace),
        _validate_trace_phase(workspace),
        _validate_timeout_phase(workspace),
        _validate_total_gate_phase(request.pre_real_report),
    )
    report = LLMActivationReadinessReport(
        ok=not any(phase.status == "FAILED" for phase in phases),
        summary=readiness_summary(phases),
        report_ref="llm_activation_readiness/report.json",
        phases=phases,
    )
    _write_json(workspace / report.report_ref, report.to_dict())
    return report


# LLM: _validate_model_adapter_phase checks provider quirks are normalized before live use.
# 函数用途: 校验 tool_call_id、stream、retry 和 model switch 结构化适配事实。
def _validate_model_adapter_phase(workspace: Path) -> LLMActivationReadinessPhase:
    facts = model_adapter_facts()
    validation = validate_model_adapter_facts(facts)
    ref = "llm_activation_readiness/model_adapter_facts.json"
    _write_json(workspace / ref, facts)
    return readiness_phase("model_adapter_entry_contract", validation.ok, [ref], list(validation.error_codes))


# LLM: _validate_prompt_context_phase checks prompt assembly preserves contract fields.
# 函数用途: 校验 assembled_context、tool schema 和非可信输入不能覆盖机器合同。
def _validate_prompt_context_phase(workspace: Path) -> LLMActivationReadinessPhase:
    facts = prompt_context_facts()
    validation = validate_prompt_context(facts)
    ref = "llm_activation_readiness/prompt_context_facts.json"
    _write_json(workspace / ref, facts)
    return readiness_phase("prompt_context_assembly_contract", validation.ok, [ref], list(validation.error_codes))


# LLM: _validate_side_effect_phase checks tool effects stay declared and replay-safe.
# 函数用途: 校验工具 effect、幂等键、dry-run/real-run 隔离和 replay 副作用阻断。
def _validate_side_effect_phase(workspace: Path) -> LLMActivationReadinessPhase:
    events = side_effect_events()
    validation = validate_side_effects(events)
    ref = "llm_activation_readiness/side_effect_events.json"
    _write_json(workspace / ref, list(events))
    return readiness_phase("tool_side_effect_gate", validation.ok, [ref], list(validation.error_codes))


# LLM: _validate_canary_phase checks canary specs stay small, isolated, and replayable.
# 函数用途: 校验小型 LLM canary 设计满足小真实 gate，不提前执行真实模型。
def _validate_canary_phase(workspace: Path) -> LLMActivationReadinessPhase:
    gate = build_small_llm_canary_gate(workspace / "llm-canary")
    validation = validate_small_real_acceptance_gate(gate)
    ref = "llm_activation_readiness/small_llm_canary_gate.json"
    _write_json(workspace / ref, gate)
    return readiness_phase("small_llm_canary_design", validation.ok, [ref], list(validation.error_codes))


# LLM: _validate_trace_phase checks live LLM runs will be replayable from structured events.
# 函数用途: 校验 state/tool trace 能通过 RunTrace 合同，并保存 capture manifest 引用。
def _validate_trace_phase(workspace: Path) -> LLMActivationReadinessPhase:
    events = trace_events()
    validation = validate_run_trace_events(events)
    manifest = trace_capture_manifest()
    events_ref = "llm_activation_readiness/trace_events.json"
    manifest_ref = "llm_activation_readiness/trace_capture_manifest.json"
    _write_json(workspace / events_ref, list(events))
    _write_json(workspace / manifest_ref, manifest)
    issues = list(validation.error_codes) + trace_manifest_issues(manifest)
    return readiness_phase("llm_trace_replay_capture", not issues, [events_ref, manifest_ref], issues)


# LLM: _validate_timeout_phase checks dynamic first-token budget is ready before live calls.
# 函数用途: 校验 5K/10K probe 估算证据已写出，且不把缓存样本当正常输入速度。
def _validate_timeout_phase(workspace: Path) -> LLMActivationReadinessPhase:
    budget = build_model_timeout_budget(workspace / "llm-timeout")
    ref = "llm-timeout/timeout_budget.json"
    issues = timeout_budget_issues(budget)
    return readiness_phase("model_timeout_input_speed_budget", not issues, [ref, str(budget["ledger_ref"])], issues)


# LLM: _validate_total_gate_phase composes all prior non-live validation facts.
# 函数用途: 校验前置 1-6 预真实任务报告通过后，才允许进入真实 LLM canary。
def _validate_total_gate_phase(
    pre_real_report: PreRealTaskValidationReport | None,
) -> LLMActivationReadinessPhase:
    if pre_real_report is None:
        return readiness_phase("pre_llm_total_gate", False, [], ["PRE_REAL_VALIDATION_MISSING"])
    if pre_real_report.ok is not True:
        return readiness_phase("pre_llm_total_gate", False, [pre_real_report.report_ref], ["PRE_REAL_VALIDATION_NOT_PASSED"])
    return readiness_phase("pre_llm_total_gate", True, [pre_real_report.report_ref], [])


# LLM: _write_json persists readiness evidence in deterministic JSON form.
# 函数用途: 写入 JSON 文件，供后续 replay、审计或 CI gate 读取。
def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


__all__ = [
    "LLMActivationReadinessPhase",
    "LLMActivationReadinessReport",
    "LLMActivationReadinessRequest",
    "build_model_timeout_budget",
    "build_small_llm_canary_gate",
    "run_llm_activation_readiness",
]
