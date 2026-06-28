
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contracts.gates.adapters import evaluate_state_transition_gate
from ...contracts.gates.artifact_gate import evaluate_delivery_closeout_gate
from ...contracts.gates.run_contract import evaluate_run_contract_gate
from ...contracts.gates.runtime_reports import (
    evaluate_acceptance_closeout_gate,
    evaluate_final_closeout_gate,
)
from .evidence import (
    fact_evidence_decision,
    source_fact_consistency_decision,
    target_coverage_projection_decision,
)
from .expected_outputs_gate import evaluate_expected_outputs_gate
from .quality import delivery_quality_decision
from .recovery import attach_contract_recovery
from .source_volume import attach_source_volume_observation
from .subagent_aggregation import evaluate_subagent_aggregation_gate
from .task_progress_gate import evaluate_task_progress_closeout_gate


@dataclass(frozen=True)
class CloseoutGateRequest:
    closeout: Any
    report: dict[str, Any]
    contract: dict[str, Any]
    workspace_root: Path


def attach_closeout_gates(request: CloseoutGateRequest) -> list[Any]:
    run_contract_decision = _attach_run_contract_gate(request)
    gate_decision = _attach_runtime_state_gates(request)
    quality_decision = _attach_quality_gate(request, run_contract_decision)
    fact_decision = _attach_fact_gate(request)
    coverage_projection_decision = _attach_target_coverage_projection_gate(request)
    source_fact_decision = _attach_source_fact_consistency_gate(request)
    task_progress_decision = _attach_task_progress_closeout_gate(request)
    expected_outputs_decision = _attach_expected_outputs_gate(request)
    subagent_decision = _attach_subagent_aggregation_gate(request)
    # 第3步折叠:派生/rollup/写死 FSM 脚手架门只写进 report 供观测(供 final rollup /
    #   failed_gate_payloads / 调试用),不再进 decisions 决策消费——
    #   acceptance(纯派生 runtime,allowed≡runtime.allowed)、final_closeout(子门 rollup,
    #   失败必由某个仍在 decisions 的子门驱动)、state(写死 RUNNING→VERIFYING 永远同一判定)。
    _attach_acceptance_gate(request, gate_decision)
    _attach_final_closeout_gate(request)
    decisions = [
        run_contract_decision,
        gate_decision,
        quality_decision,
        fact_decision,
        coverage_projection_decision,
        source_fact_decision,
        task_progress_decision,
        expected_outputs_decision,
        subagent_decision,
    ]
    attach_contract_recovery(request.report, decisions, contract=request.contract)
    return decisions


def _attach_run_contract_gate(request: CloseoutGateRequest) -> Any:
    closeout = request.closeout
    decision = evaluate_run_contract_gate(
        request.contract,
        scope={
            "request_id": closeout.params.request_id,
            "run_id": closeout.params.run_id,
            "task_id": closeout.params.task_id,
            "workspace_root": str(request.workspace_root),
        },
    )
    request.report["run_contract_gate"] = decision.to_dict()
    return decision


def _attach_runtime_state_gates(request: CloseoutGateRequest) -> Any:
    gate_decision = evaluate_delivery_closeout_gate(request.report)
    request.report["runtime_gate"] = gate_decision.to_dict()
    # state_gate:写死 RUNNING→VERIFYING 的 FSM 脚手架(永远同一判定),第3步折叠——
    #   只写进 report 供观测/final rollup,不再进 decisions 决策消费。
    state_decision = evaluate_state_transition_gate("RUNNING", "VERIFYING")
    request.report["state_gate"] = state_decision.to_dict()
    return gate_decision


def _attach_quality_gate(request: CloseoutGateRequest, run_contract_decision: Any) -> Any:
    contract_hash = str(run_contract_decision.evidence.get("effective_contract_hash") or "")
    decision = delivery_quality_decision(
        contract=request.contract,
        report=request.report,
        workspace_root=request.workspace_root,
        contract_hash=contract_hash,
    )
    request.report["delivery_quality_gate"] = decision.to_dict()
    return decision


def _attach_fact_gate(request: CloseoutGateRequest) -> Any:
    archive_calls = getattr(request.closeout.params, "archive_tool_calls", []) or []
    decision = fact_evidence_decision(
        contract=request.contract,
        workspace_root=request.workspace_root,
        archive_tool_calls=[item for item in archive_calls if isinstance(item, dict)],
    )
    request.report["fact_evidence_gate"] = decision.to_dict()
    return decision


def _attach_target_coverage_projection_gate(request: CloseoutGateRequest) -> Any:
    decision = target_coverage_projection_decision(request.report)
    request.report["target_coverage_projection_gate"] = decision.to_dict()
    return decision


def _attach_source_fact_consistency_gate(request: CloseoutGateRequest) -> Any:
    decision = source_fact_consistency_decision(request.report, workspace_root=request.workspace_root)
    request.report["source_fact_consistency_gate"] = decision.to_dict()
    return decision


def _attach_subagent_aggregation_gate(request: CloseoutGateRequest) -> Any:
    decision = evaluate_subagent_aggregation_gate(request.closeout)
    request.report["subagent_aggregation_gate"] = decision.to_dict()
    return decision


def _attach_task_progress_closeout_gate(request: CloseoutGateRequest) -> Any:
    decision = evaluate_task_progress_closeout_gate(request.closeout, request.report)
    request.report["task_progress_closeout_gate"] = decision.to_dict()
    return decision


# 函数用途: 产物类型/数量对账门(声明驱动):核对 expected_outputs 声明与交付区实存。
def _attach_expected_outputs_gate(request: CloseoutGateRequest) -> Any:
    decision = evaluate_expected_outputs_gate(request.closeout)
    request.report["expected_outputs_gate"] = decision.to_dict()
    # 来源比例观测(R8b 隐蔽编造实锤):检索量 vs 交付量并排数字,纯观测零判定。
    attach_source_volume_observation(request.closeout, request.report)
    return decision


def _attach_acceptance_gate(request: CloseoutGateRequest, gate_decision: Any) -> Any:
    decision = evaluate_acceptance_closeout_gate(
        {
            "final_status": "DONE",
            "verification_status": "PASSED" if gate_decision.allowed else "FAILED",
            "runtime_gate": gate_decision.to_dict(),
        }
    )
    request.report["acceptance_gate"] = decision.to_dict()
    return decision


# 第3步折叠:final_closeout 是其余子门(run_contract/runtime/state/quality/fact/acceptance)
#   的 rollup,只写进 report 供观测,不再进 decisions——它失败必由某个仍在 decisions 的
#   子门驱动,故移出后 _closeout_decision 的 L1 阻断与 advisory 注入均不变。
def _attach_final_closeout_gate(request: CloseoutGateRequest) -> Any:
    decision = evaluate_final_closeout_gate(request.report)
    request.report["final_closeout_gate"] = decision.to_dict()
    return decision
