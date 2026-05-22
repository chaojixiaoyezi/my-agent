# LLM: Gate pipeline binds runtime phase/action pairs to ordered mandatory gates.
# 模块用途: 用结构化 spec 明确每个 phase/action 要经过哪些 gate，避免高危入口因空注册或自然语言说明被误放行。

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .gate_pipeline_decisions import (
    PipelineProgress,
    action_for_context,
    blocked_pipeline_decision,
    dependency_decision,
    gate_context,
    missing_gate_decision,
    missing_spec_decision,
    pipeline_config_decision,
)
from .gate_pipeline_specs import (
    DEFAULT_GATE_PIPELINE_SPECS,
    DEFAULT_HIGH_RISK_PHASES,
    GatePipelineSpec,
    GatePipelineStep,
    key,
    required_key,
)
from .models import GateContext, GateDecision, GateFinding, GateValidator
from .registry import GateRegistry


# LLM: GatePipeline evaluates the mandatory gate sequence for one runtime phase/action.
# 类用途: 在已有 GateRegistry 之上增加 fail-closed spec、依赖检查、短路和结构化汇总决策。
@dataclass
class GatePipeline:
    registry: GateRegistry = field(default_factory=GateRegistry)
    specs: Iterable[GatePipelineSpec] | None = None
    high_risk_phases: Iterable[str] = DEFAULT_HIGH_RISK_PHASES

    # LLM: __post_init__ keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    def __post_init__(self) -> None:
        specs = DEFAULT_GATE_PIPELINE_SPECS if self.specs is None else tuple(self.specs)
        self._specs: dict[tuple[str, str], GatePipelineSpec] = {}
        self._high_risk_phases = {key(item) for item in self.high_risk_phases if key(item)}
        for spec in specs:
            self.register_spec(spec)

    # LLM: GatePipeline.register forwards validators into the existing registry by gate name.
    # 函数用途: 允许调用方沿用 GateRegistry 注册 validator，再由 pipeline 负责 phase/action 编排。
    def register(self, gate: str, validator: GateValidator) -> None:
        self.registry.register(required_key(gate, field_name="gate"), validator)

    # LLM: GatePipeline.register_spec adds one phase/action pipeline and records high-risk phases.
    # 函数用途: 注册明确的 gate 顺序；高危 spec 会让缺失配置失败关闭。
    def register_spec(self, spec: GatePipelineSpec) -> None:
        self._specs[(spec.phase, spec.action)] = spec
        if spec.high_risk:
            self._high_risk_phases.add(spec.phase)

    # LLM: GatePipeline.evaluate returns one structured decision for the whole pipeline.
    # 函数用途: 按 spec 顺序执行必需 gate，缺失或阻断时返回 GateDecision/GateFinding 和 recommended_action。
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
                recommended_action="register_required_gate",
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

    # LLM: _spec_for keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    def _spec_for(self, phase: str, action: str) -> GatePipelineSpec | None:
        return self._specs.get((phase, action)) or self._specs.get((phase, "*")) or self._specs.get((phase, "default"))


__all__ = [
    "DEFAULT_GATE_PIPELINE_SPECS",
    "DEFAULT_HIGH_RISK_PHASES",
    "GatePipeline",
    "GatePipelineSpec",
    "GatePipelineStep",
]
