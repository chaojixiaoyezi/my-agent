from __future__ import annotations

from agent_py_agent.agent.contracts.gates import (
    GateContext,
    GateDecision,
    GateFinding,
    GateRegistry,
)
from agent_py_agent.agent.contracts.gates.gate_pipeline import (
    GatePipeline,
    GatePipelineSpec,
    GatePipelineStep,
)


def test_gate_pipeline_runs_phase_action_gates_in_declared_order():
    calls: list[str] = []
    registry = GateRegistry()
    for gate in ("tool_call", "path_url_command", "tool_effect"):
        registry.register(gate, _allowing_gate(gate, calls))
    pipeline = GatePipeline(
        registry=registry,
        specs=(
            GatePipelineSpec(
                phase="tool_execution",
                action="mutating",
                steps=(
                    GatePipelineStep("tool_call"),
                    GatePipelineStep("path_url_command", depends_on=("tool_call",)),
                    GatePipelineStep("tool_effect", depends_on=("path_url_command",), side_effect=True),
                ),
            ),
        ),
    )

    decision = pipeline.evaluate(GateContext(phase="tool_execution", payload={"tool": "write_file"}), action="mutating")

    assert decision.allowed is True
    assert calls == ["tool_call", "path_url_command", "tool_effect"]
    assert decision.evidence["planned_gates"] == ["tool_call", "path_url_command", "tool_effect"]
    assert decision.evidence["executed_gates"] == ["tool_call", "path_url_command", "tool_effect"]


def test_gate_pipeline_fails_closed_when_required_gate_is_missing():
    calls: list[str] = []
    registry = GateRegistry()
    registry.register("tool_call", _allowing_gate("tool_call", calls))
    pipeline = GatePipeline(
        registry=registry,
        specs=(
            GatePipelineSpec(
                phase="tool_execution",
                action="mutating",
                steps=(
                    GatePipelineStep("tool_call"),
                    GatePipelineStep("path_url_command", depends_on=("tool_call",)),
                ),
            ),
        ),
    )

    decision = pipeline.evaluate(GateContext(phase="tool_execution", payload={"tool": "write_file"}), action="mutating")

    assert decision.allowed is False
    assert decision.finding_codes == ("GATE_PIPELINE_REQUIRED_GATE_MISSING",)
    assert decision.recommended_action == "register_required_gate"
    assert decision.evidence["missing_gate"] == "path_url_command"
    assert decision.evidence["executed_gates"] == ["tool_call"]
    assert calls == ["tool_call"]


def test_gate_pipeline_short_circuits_before_later_side_effect_gate():
    calls: list[str] = []
    registry = GateRegistry()
    registry.register("tool_call", _blocking_gate("tool_call", calls))
    registry.register("tool_effect", _allowing_gate("tool_effect", calls))
    pipeline = GatePipeline(
        registry=registry,
        specs=(
            GatePipelineSpec(
                phase="tool_execution",
                action="dangerous",
                steps=(
                    GatePipelineStep("tool_call"),
                    GatePipelineStep("tool_effect", depends_on=("tool_call",), side_effect=True),
                ),
                short_circuit=True,
            ),
        ),
    )

    decision = pipeline.evaluate(GateContext(phase="tool_execution", payload={"tool": "controlled_exec"}), action="dangerous")

    assert decision.allowed is False
    assert decision.finding_codes == ("TOOL_CALL_BAD_FACT",)
    assert decision.recommended_action == "repair_tool_call"
    assert decision.evidence["executed_gates"] == ["tool_call"]
    assert decision.evidence["skipped_gates"] == ["tool_effect"]
    assert calls == ["tool_call"]


def test_gate_pipeline_does_not_allow_empty_high_risk_phase():
    pipeline = GatePipeline(registry=GateRegistry(), specs=())

    decision = pipeline.evaluate(GateContext(phase="tool_execution", payload={"tool": "write_file"}), action="mutating")

    assert decision.allowed is False
    assert decision.finding_codes == ("GATE_PIPELINE_SPEC_MISSING",)
    assert decision.recommended_action == "register_gate_pipeline"
    assert decision.evidence["phase"] == "tool_execution"
    assert decision.evidence["action"] == "mutating"


def _allowing_gate(gate: str, calls: list[str]):
    def _validator(context: GateContext) -> GateDecision:
        calls.append(gate)
        return GateDecision.allow(gate, evidence={"pipeline_phase": context.refs["pipeline"]["phase"]})

    return _validator


def _blocking_gate(gate: str, calls: list[str]):
    def _validator(context: GateContext) -> GateDecision:
        calls.append(gate)
        return GateDecision.repair(
            gate,
            [GateFinding("TOOL_CALL_BAD_FACT")],
            recommended_action="repair_tool_call",
        )

    return _validator
