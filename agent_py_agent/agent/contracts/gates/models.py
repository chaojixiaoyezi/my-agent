# LLM: Runtime gate models are the common decision shape for mandatory contract checkpoints.
# 模块用途: 定义运行时“门”的统一输入、finding 和决策结果，避免各入口各写一套 ok/error 文本。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..recovery_envelope import RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload

GateValidator = Callable[["GateContext"], "GateDecision"]


# LLM: GateFinding is a machine-readable reason emitted by one runtime gate.
# 类用途: 保存稳定错误码、严重级别和结构化证据；不把自然语言当机器事实来源。
@dataclass(frozen=True)
class GateFinding:
    code: str
    severity: str = "P1"
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    # LLM: GateFinding.to_dict is the stable serialized finding shape.
    # 函数用途: 输出 code/severity/message/evidence 结构字段，供报告、审计和测试读取。
    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


# LLM: GateContext carries the structured facts a gate may inspect.
# 类用途: 保存阶段名、run/task/request 标识、payload 和 refs，供 gate 只读机器字段。
@dataclass(frozen=True)
class GateContext:
    phase: str
    payload: Any = None
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    contract: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, Any] = field(default_factory=dict)


# LLM: GateDecision is the only result shape consumed by runtime gate callers.
# 类用途: 表示 ALLOW/DENY/NEED_REPAIR/NEED_APPROVAL/BLOCKED/RECOVERING 等门决策。
@dataclass(frozen=True)
class GateDecision:
    gate: str
    status: str
    allowed: bool
    findings: tuple[GateFinding, ...] = ()
    recommended_action: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    # LLM: GateDecision.finding_codes gives tests and callers a compact machine code list.
    # 函数用途: 从 finding 结构中提取 code，不解析 message 文本。
    @property
    def finding_codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)

    # LLM: GateDecision.allow constructs an allowed runtime gate decision.
    # 函数用途: 统一生成 ALLOW 状态，并保留结构化 evidence。
    @classmethod
    def allow(
        cls,
        gate: str,
        *,
        recommended_action: str = "continue",
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(gate, "ALLOW", True, recommended_action=recommended_action, evidence=evidence or {})

    # LLM: GateDecision.deny constructs a hard denial with one machine finding.
    # 函数用途: 统一生成 DENY 状态，并用 code/evidence 表达阻断原因。
    @classmethod
    def deny(
        cls,
        gate: str,
        code: str,
        *,
        evidence: dict[str, Any] | None = None,
        recommended_action: str = "stop",
    ) -> GateDecision:
        return cls(
            gate,
            "DENY",
            False,
            (GateFinding(code, evidence=evidence or {}),),
            recommended_action=recommended_action,
        )

    # LLM: GateDecision.repair constructs a repairable gate failure.
    # 函数用途: 表示产物或合同可修复，调用方应继续修复而非假完成。
    @classmethod
    def repair(
        cls,
        gate: str,
        findings: list[GateFinding] | tuple[GateFinding, ...],
        *,
        recommended_action: str = "repair",
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(gate, "NEED_REPAIR", False, tuple(findings), recommended_action, evidence or {})

    # LLM: GateDecision.need_approval constructs an approval-required decision.
    # 函数用途: 表示真实副作用需要审批，不能继续自动执行。
    @classmethod
    def need_approval(
        cls,
        gate: str,
        code: str = "APPROVAL_REQUIRED",
        *,
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(
            gate,
            "NEED_APPROVAL",
            False,
            (GateFinding(code, evidence=evidence or {}),),
            "request_approval",
            {},
        )

    # LLM: GateDecision.block constructs a blocked runtime decision.
    # 函数用途: 表示当前状态不能继续自动推进，需要停止或恢复。
    @classmethod
    def block(
        cls,
        gate: str,
        code: str,
        *,
        evidence: dict[str, Any] | None = None,
        recommended_action: str = "stop_or_recover",
    ) -> GateDecision:
        return cls(
            gate,
            "BLOCKED",
            False,
            (GateFinding(code, evidence=evidence or {}),),
            recommended_action,
            {},
        )

    # LLM: GateDecision.recovering constructs a recovery-required decision.
    # 函数用途: 表示账本、恢复快照或审计记录缺关键字段，需要从 checkpoint 修复。
    @classmethod
    def recovering(
        cls,
        gate: str,
        findings: list[GateFinding] | tuple[GateFinding, ...],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(gate, "RECOVERING", False, tuple(findings), "recover_from_checkpoint", evidence or {})

    # LLM: GateDecision.to_dict is the stable serialized gate result.
    # 函数用途: 输出 gate/status/allowed/findings/recommended_action/evidence；失败时附 recovery 返工包供日志、replay 和模型反馈读取。
    def to_dict(self) -> dict[str, Any]:
        payload = {
            "gate": self.gate,
            "status": self.status,
            "allowed": self.allowed,
            "findings": [finding.to_dict() for finding in self.findings],
            "recommended_action": self.recommended_action,
            "evidence": dict(self.evidence),
        }
        recovery = recovery_envelope_from_gate_payload(
            RecoveryEnvelopeRequest(
                gate=self.gate,
                status=self.status,
                allowed=self.allowed,
                findings=payload["findings"],
                recommended_action=self.recommended_action,
                evidence=payload["evidence"],
            )
        )
        if recovery is not None:
            payload["recovery"] = recovery.to_dict()
        return payload


__all__ = ["GateContext", "GateDecision", "GateFinding", "GateValidator"]
