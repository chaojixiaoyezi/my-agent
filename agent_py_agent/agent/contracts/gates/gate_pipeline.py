from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..recovery import RecoveryAction
from .models import GateContext, GateDecision, GateFinding, GateValidator
from .registry import GateRegistry

DEFAULT_HIGH_RISK_PHASES = frozenset({
    "tool_execution",
    "recovery",
    "recovery_replay",
    "runtime_audit",
    "state_transition",
})

@dataclass(frozen=True)
class GatePipelineStep:
    gate: str
    depends_on: tuple[str, ...] = ()
    side_effect: bool = False
    def __post_init__(self) -> None:
        object.__setattr__(self, "gate", required_key(self.gate, field_name="gate"))
        object.__setattr__(self, "depends_on", tuple(key(item) for item in self.depends_on if key(item)))

@dataclass(frozen=True)
class GatePipelineSpec:
    phase: str
    action: str
    steps: tuple[GatePipelineStep | str, ...] = ()
    short_circuit: bool = True
    high_risk: bool = True
    def __post_init__(self) -> None:
        steps = tuple(pipeline_step(item) for item in self.steps)
        validate_steps(steps)
        object.__setattr__(self, "phase", required_key(self.phase, field_name="phase"))
        object.__setattr__(self, "action", required_key(self.action, field_name="action"))
        object.__setattr__(self, "steps", steps)

@dataclass(frozen=True)
class PipelineProgress:
    phase: str
    action: str
    planned: tuple[str, ...]
    executed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    def with_executed(self, gate: str) -> PipelineProgress:
        return replace(self, executed=(*self.executed, gate))
    def with_skipped(self, gates: tuple[str, ...]) -> PipelineProgress:
        return replace(self, skipped=(*self.skipped, *gates))
    def evidence(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "action": self.action,
            "planned_gates": list(self.planned),
            "executed_gates": list(self.executed),
            "skipped_gates": list(self.skipped),
        }

def pipeline_step(item: GatePipelineStep | str) -> GatePipelineStep:
    return item if isinstance(item, GatePipelineStep) else GatePipelineStep(str(item))

def validate_steps(steps: Iterable[GatePipelineStep]) -> None:
    seen: set[str] = set()
    for step in steps:
        if step.gate in seen:
            raise ValueError(f"duplicate gate in pipeline spec: {step.gate}")
        missing = [gate for gate in step.depends_on if gate not in seen]
        if missing:
            raise ValueError(f"gate {step.gate} depends on gates that must appear earlier: {', '.join(missing)}")
        seen.add(step.gate)

def required_key(value: object, *, field_name: str) -> str:
    item = key(value)
    if not item:
        raise ValueError(f"gate pipeline {field_name} is required")
    return item

def key(value: object) -> str:
    return str(value or "").strip()

_TOOL_BASE_STEPS = (
    GatePipelineStep("tool_call"),
    GatePipelineStep("tool_manifest", depends_on=("tool_call",)),
    GatePipelineStep("path_url_command", depends_on=("tool_call", "tool_manifest")),
    GatePipelineStep("tool_guardrail", depends_on=("path_url_command",)),
    GatePipelineStep("tool_rate_limit", depends_on=("tool_guardrail",)),
)

def _tool_pipeline_spec(action: str, *, side_effect: bool = False) -> GatePipelineSpec:
    effect_steps = (GatePipelineStep("tool_effect", depends_on=("tool_rate_limit",), side_effect=True),)
    return GatePipelineSpec(
        phase="tool_execution",
        action=action,
        steps=(*_TOOL_BASE_STEPS, *(effect_steps if side_effect else ())),
    )

DEFAULT_GATE_PIPELINE_SPECS = (
    _tool_pipeline_spec("read_only"),
    _tool_pipeline_spec("mutating", side_effect=True),
    _tool_pipeline_spec("dangerous", side_effect=True),
    GatePipelineSpec(
        phase="recovery",
        action="replay",
        steps=(
            GatePipelineStep("recovery_replay"),
            GatePipelineStep("runtime_audit", depends_on=("recovery_replay",)),
        ),
    ),
)

def gate_context(context: GateContext, progress: PipelineProgress, step: GatePipelineStep, index: int) -> GateContext:
    refs = dict(context.refs)
    refs["pipeline"] = {"phase": progress.phase, "action": progress.action, "gate": step.gate, "index": index}
    return replace(context, phase=step.gate, refs=refs)

def dependency_decision(
    step: GatePipelineStep,
    allowed_gates: set[str],
    progress: PipelineProgress,
) -> GateDecision | None:
    missing = [gate for gate in step.depends_on if gate not in allowed_gates]
    if not missing:
        return None
    return pipeline_config_decision(
        "GATE_PIPELINE_DEPENDENCY_UNSATISFIED",
        progress,
        recommended_action=RecoveryAction.REPAIR_GATE_PIPELINE_ORDER.value,
        evidence={"gate": step.gate, "missing_dependencies": missing},
    )

def missing_spec_decision(phase: str, action: str, *, high_risk: bool) -> GateDecision:
    if not high_risk:
        return GateDecision.allow(
            "gate_pipeline",
            evidence={"phase": phase, "action": action, "pipeline": "not_required", "gate_count": 0},
        )
    progress = PipelineProgress(phase, action, ())
    return pipeline_config_decision(
        "GATE_PIPELINE_SPEC_MISSING",
        progress,
        recommended_action=RecoveryAction.REGISTER_GATE_PIPELINE.value,
        evidence={},
    )

def missing_gate_decision(gate: str, progress: PipelineProgress) -> GateDecision:
    remaining = tuple(item for item in progress.planned if item not in progress.executed and item != gate)
    updated = progress.with_skipped(remaining)
    return pipeline_config_decision(
        "GATE_PIPELINE_REQUIRED_GATE_MISSING",
        updated,
        recommended_action=RecoveryAction.REGISTER_REQUIRED_GATE.value,
        evidence={"missing_gate": gate},
    )

def pipeline_config_decision(
    code: str,
    progress: PipelineProgress,
    *,
    recommended_action: str,
    evidence: dict[str, Any],
) -> GateDecision:
    payload = {**progress.evidence(), **evidence}
    return GateDecision(
        "gate_pipeline",
        "DENY",
        False,
        (GateFinding(code, evidence=payload),),
        recommended_action,
        payload,
    )

def blocked_pipeline_decision(decisions: list[GateDecision], progress: PipelineProgress) -> GateDecision:
    evidence = progress.evidence() | {
        "blocked_gates": [decision.gate for decision in decisions],
        "child_statuses": [
            {"gate": decision.gate, "status": decision.status, "allowed": decision.allowed}
            for decision in decisions
        ],
    }
    if len(decisions) == 1:
        child = decisions[0]
        return replace(child, evidence={**dict(child.evidence), **evidence})
    return GateDecision(
        "gate_pipeline",
        _merged_status(decisions),
        False,
        tuple(_child_findings(decisions)),
        _merged_action(decisions),
        evidence,
    )

def action_for_context(context: GateContext, action: str) -> str:
    if key(action):
        return key(action)
    for source in (context.scope, context.contract, context.refs):
        value = _mapping_action(source)
        if value:
            return value
    if isinstance(context.payload, Mapping):
        value = _mapping_action(context.payload)
        if value:
            return value
    return "default"

def _mapping_action(source: Mapping[str, Any]) -> str:
    for item_key in ("gate_action", "action", "operation", "operation_kind"):
        value = key(source.get(item_key))
        if value:
            return value
    return ""

def _child_findings(decisions: list[GateDecision]) -> list[GateFinding]:
    findings: list[GateFinding] = []
    for decision in decisions:
        rows = decision.findings or (GateFinding("GATE_PIPELINE_CHILD_BLOCKED"),)
        findings.extend(
            GateFinding(
                finding.code,
                severity=finding.severity,
                message=finding.message,
                evidence={"child_gate": decision.gate, **finding.evidence},
            )
            for finding in rows
        )
    return findings

def _merged_status(decisions: list[GateDecision]) -> str:
    for status in ("DENY", "BLOCKED", "NEED_APPROVAL", "NEED_REPAIR", "RECOVERING"):
        if any(decision.status == status for decision in decisions):
            return status
    return decisions[0].status if decisions else "DENY"

def _merged_action(decisions: list[GateDecision]) -> str:
    for decision in decisions:
        if decision.recommended_action:
            return decision.recommended_action
    return RecoveryAction.REPORT_BLOCKER.value

@dataclass
class GatePipeline:
    registry: GateRegistry = field(default_factory=GateRegistry)
    specs: Iterable[GatePipelineSpec] | None = None
    high_risk_phases: Iterable[str] = DEFAULT_HIGH_RISK_PHASES
    def __post_init__(self) -> None:
        specs = DEFAULT_GATE_PIPELINE_SPECS if self.specs is None else tuple(self.specs)
        self._specs: dict[tuple[str, str], GatePipelineSpec] = {}
        self._high_risk_phases = {key(item) for item in self.high_risk_phases if key(item)}
        for spec in specs:
            self.register_spec(spec)
    def register(self, gate: str, validator: GateValidator) -> None:
        self.registry.register(required_key(gate, field_name="gate"), validator)
    def register_spec(self, spec: GatePipelineSpec) -> None:
        self._specs[(spec.phase, spec.action)] = spec
        if spec.high_risk:
            self._high_risk_phases.add(spec.phase)
    def evaluate(self, context: GateContext, *, action: str = "") -> GateDecision:
        phase = required_key(context.phase, field_name="phase")
        action_key = action_for_context(context, action)
        spec = self._spec_for(phase, action_key)
        if spec is None:
            return missing_spec_decision(phase, action_key, high_risk=phase in self._high_risk_phases)
        if not spec.steps:
            return pipeline_config_decision(
                "GATE_PIPELINE_EMPTY",
                PipelineProgress(phase, action_key, ()),
                recommended_action=RecoveryAction.REGISTER_REQUIRED_GATE.value,
                evidence={"planned_gates": []},
            )
        progress = PipelineProgress(phase, action_key, tuple(step.gate for step in spec.steps))
        allowed_gates: set[str] = set()
        blocking: list[GateDecision] = []
        for index, step in enumerate(spec.steps):
            if blocking and step.side_effect:
                progress = progress.with_skipped((step.gate,))
                continue
            if decision := dependency_decision(step, allowed_gates, progress):
                return decision
            if not self.registry.has_validators(step.gate):
                return missing_gate_decision(step.gate, progress)
            decision = self.registry.evaluate(gate_context(context, progress, step, index))
            progress = progress.with_executed(step.gate)
            if decision.allowed:
                allowed_gates.add(step.gate)
                continue
            blocking.append(decision)
            if spec.short_circuit:
                progress = progress.with_skipped(tuple(item.gate for item in spec.steps[index + 1 :]))
                break
        if blocking:
            return blocked_pipeline_decision(blocking, progress)
        return GateDecision.allow(
            "gate_pipeline",
            evidence={**progress.evidence(), "gate_count": len(progress.executed)},
        )
    def _spec_for(self, phase: str, action: str) -> GatePipelineSpec | None:
        return self._specs.get((phase, action)) or self._specs.get((phase, "*")) or self._specs.get((phase, "default"))

__all__ = [
    "DEFAULT_GATE_PIPELINE_SPECS",
    "DEFAULT_HIGH_RISK_PHASES",
    "GatePipeline",
    "GatePipelineSpec",
    "GatePipelineStep",
]
