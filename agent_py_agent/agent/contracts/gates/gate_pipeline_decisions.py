# LLM: Gate pipeline decision helpers merge child gate results into one structured runtime decision.
# 模块用途: 统一流水线缺 spec、缺 gate、依赖失败和子 gate 阻断的 GateDecision 形状。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .gate_pipeline_specs import GatePipelineStep, key
from .models import GateContext, GateDecision, GateFinding


# LLM: PipelineProgress carries one pipeline evaluation snapshot.
# 类用途: 把 phase/action/planned/executed/skipped 打包，降低 helper 参数数量并保持证据一致。
@dataclass(frozen=True)
class PipelineProgress:
    phase: str
    action: str
    planned: tuple[str, ...]
    executed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()

    # LLM: with_executed returns a new immutable progress snapshot.
    # 函数用途: 追加已执行 gate，避免多个 helper 修改同一个列表对象。
    def with_executed(self, gate: str) -> PipelineProgress:
        return replace(self, executed=(*self.executed, gate))

    # LLM: with_skipped returns a new immutable progress snapshot.
    # 函数用途: 追加被跳过 gate，保留后续决策需要的结构化证据。
    def with_skipped(self, gates: tuple[str, ...]) -> PipelineProgress:
        return replace(self, skipped=(*self.skipped, *gates))

    # LLM: evidence returns the common pipeline evidence shape.
    # 函数用途: 为 allow/block/config finding 生成同一套 planned/executed/skipped 字段。
    def evidence(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "action": self.action,
            "planned_gates": list(self.planned),
            "executed_gates": list(self.executed),
            "skipped_gates": list(self.skipped),
        }


# LLM: gate_context preserves original pipeline facts while running one child validator.
# 函数用途: GateRegistry 仍按 gate 名找 validator，同时 validator 可读取 pipeline refs 的原始 phase/action。
def gate_context(context: GateContext, progress: PipelineProgress, step: GatePipelineStep, index: int) -> GateContext:
    refs = dict(context.refs)
    refs["pipeline"] = {"phase": progress.phase, "action": progress.action, "gate": step.gate, "index": index}
    return replace(context, phase=step.gate, refs=refs)


# LLM: dependency_decision blocks a step whose declared prerequisites did not pass.
# 函数用途: 只按 depends_on 和已放行 gate 集合判断，返回可修复的结构化配置 finding。
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
        recommended_action="repair_gate_pipeline_order",
        evidence={"gate": step.gate, "missing_dependencies": missing},
    )


# LLM: missing_spec_decision fails closed for high-risk phases without a registered pipeline.
# 函数用途: 没有 phase/action spec 时，高危 phase 阻断；低危 phase 记录 not_required。
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
        recommended_action="register_gate_pipeline",
        evidence={},
    )


# LLM: missing_gate_decision blocks when a required gate has no validator.
# 函数用途: 防止高危入口因为 registry 为空而被误放行，并返回剩余未执行 gate。
def missing_gate_decision(gate: str, progress: PipelineProgress) -> GateDecision:
    remaining = tuple(item for item in progress.planned if item not in progress.executed and item != gate)
    updated = progress.with_skipped(remaining)
    return pipeline_config_decision(
        "GATE_PIPELINE_REQUIRED_GATE_MISSING",
        updated,
        recommended_action="register_required_gate",
        evidence={"missing_gate": gate},
    )


# LLM: pipeline_config_decision creates one stable config-deny GateDecision.
# 函数用途: 给缺 spec、缺 gate、依赖错误等配置问题生成统一 finding 和 recommended_action。
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


# LLM: blocked_pipeline_decision merges one or more child-deny decisions.
# 函数用途: 单个子 gate 阻断时保留原 gate code，多 gate 阻断时统一汇总。
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


# LLM: action_for_context selects a structured gate action from scope/contract/refs/payload.
# 函数用途: 只读取机器字段 gate_action/action/operation，不解析自然语言任务描述。
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


# LLM: _mapping_action reads the supported action aliases from one mapping.
# 函数用途: 兼容 gate_action/action/operation/operation_kind 这些结构字段。
def _mapping_action(source: Mapping[str, Any]) -> str:
    for item_key in ("gate_action", "action", "operation", "operation_kind"):
        value = key(source.get(item_key))
        if value:
            return value
    return ""


# LLM: _child_findings tags child gate findings with the blocked child gate.
# 函数用途: 多 gate 汇总时保留原 finding code，并补充 child_gate 证据。
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


# LLM: _merged_status returns the strongest child gate status.
# 函数用途: 多 gate 阻断时按拒绝、阻塞、审批、返工、恢复的顺序合并状态。
def _merged_status(decisions: list[GateDecision]) -> str:
    for status in ("DENY", "BLOCKED", "NEED_APPROVAL", "NEED_REPAIR", "RECOVERING"):
        if any(decision.status == status for decision in decisions):
            return status
    return decisions[0].status if decisions else "DENY"


# LLM: _merged_action keeps the first actionable child recommendation.
# 函数用途: 多 gate 阻断时保留具体 recommended_action，缺失时返回 stop_or_recover。
def _merged_action(decisions: list[GateDecision]) -> str:
    for decision in decisions:
        if decision.recommended_action:
            return decision.recommended_action
    return "stop_or_recover"


__all__ = [
    "PipelineProgress",
    "action_for_context",
    "blocked_pipeline_decision",
    "dependency_decision",
    "gate_context",
    "missing_gate_decision",
    "missing_spec_decision",
    "pipeline_config_decision",
]
