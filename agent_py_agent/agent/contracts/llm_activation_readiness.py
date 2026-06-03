
from __future__ import annotations

from pathlib import Path

from ..common.json_io import write_json_file
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
    write_json_file(workspace / report.report_ref, report.to_dict())
    return report


def _validate_model_adapter_phase(workspace: Path) -> LLMActivationReadinessPhase:
    facts = model_adapter_facts()
    validation = validate_model_adapter_facts(facts)
    ref = "llm_activation_readiness/model_adapter_facts.json"
    write_json_file(workspace / ref, facts)
    return readiness_phase("model_adapter_entry_contract", validation.ok, [ref], list(validation.error_codes))


def _validate_prompt_context_phase(workspace: Path) -> LLMActivationReadinessPhase:
    facts = prompt_context_facts()
    validation = validate_prompt_context(facts)
    ref = "llm_activation_readiness/prompt_context_facts.json"
    write_json_file(workspace / ref, facts)
    return readiness_phase("prompt_context_assembly_contract", validation.ok, [ref], list(validation.error_codes))


def _validate_side_effect_phase(workspace: Path) -> LLMActivationReadinessPhase:
    events = side_effect_events()
    validation = validate_side_effects(events)
    ref = "llm_activation_readiness/side_effect_events.json"
    write_json_file(workspace / ref, list(events))
    return readiness_phase("tool_side_effect_gate", validation.ok, [ref], list(validation.error_codes))


def _validate_canary_phase(workspace: Path) -> LLMActivationReadinessPhase:
    gate = build_small_llm_canary_gate(workspace / "llm-canary")
    validation = validate_small_real_acceptance_gate(gate)
    ref = "llm_activation_readiness/small_llm_canary_gate.json"
    write_json_file(workspace / ref, gate)
    return readiness_phase("small_llm_canary_design", validation.ok, [ref], list(validation.error_codes))


def _validate_trace_phase(workspace: Path) -> LLMActivationReadinessPhase:
    events = trace_events()
    validation = validate_run_trace_events(events)
    manifest = trace_capture_manifest()
    events_ref = "llm_activation_readiness/trace_events.json"
    manifest_ref = "llm_activation_readiness/trace_capture_manifest.json"
    write_json_file(workspace / events_ref, list(events))
    write_json_file(workspace / manifest_ref, manifest)
    issues = list(validation.error_codes) + trace_manifest_issues(manifest)
    return readiness_phase("llm_trace_replay_capture", not issues, [events_ref, manifest_ref], issues)


def _validate_timeout_phase(workspace: Path) -> LLMActivationReadinessPhase:
    budget = build_model_timeout_budget(workspace / "llm-timeout")
    ref = "llm-timeout/timeout_budget.json"
    issues = timeout_budget_issues(budget)
    return readiness_phase("model_timeout_input_speed_budget", not issues, [ref, str(budget["ledger_ref"])], issues)


def _validate_total_gate_phase(
    pre_real_report: PreRealTaskValidationReport | None,
) -> LLMActivationReadinessPhase:
    if pre_real_report is None:
        return readiness_phase("pre_llm_total_gate", False, [], ["PRE_REAL_VALIDATION_MISSING"])
    if pre_real_report.ok is not True:
        return readiness_phase("pre_llm_total_gate", False, [pre_real_report.report_ref], ["PRE_REAL_VALIDATION_NOT_PASSED"])
    return readiness_phase("pre_llm_total_gate", True, [pre_real_report.report_ref], [])


__all__ = [
    "LLMActivationReadinessPhase",
    "LLMActivationReadinessReport",
    "LLMActivationReadinessRequest",
    "build_model_timeout_budget",
    "build_small_llm_canary_gate",
    "run_llm_activation_readiness",
]
