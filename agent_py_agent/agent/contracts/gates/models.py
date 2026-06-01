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

    @property
    def allow_action(self) -> bool:
        """Return whether the current attempted action may run."""
        return self.allowed

    @property
    def block_task(self) -> bool:
        """Return True only for explicit terminal-task decisions."""
        return self.recommended_action == "terminal_block" or self.evidence.get("block_task") is True

    @property
    def severity(self) -> str:
        """Classify the gate for operators without changing its status semantics."""
        if self.allowed and not self.findings:
            return "info"
        if self.gate in _SAFETY_GATES or any(_safety_code(item.code) for item in self.findings):
            return "safety"
        if self.gate in _QUALITY_GATES or self.status == "NEED_REPAIR":
            return "quality"
        return "engineering"

    @property
    def model_message(self) -> str:
        """Short model-facing repair or steering message."""
        if self.allowed and not self.findings:
            return ""
        messages = [finding.message for finding in self.findings if finding.message]
        if messages:
            return "；".join(messages)[:500]
        codes = ", ".join(self.finding_codes)
        if self.allowed:
            return f"{self.gate} 提醒：{codes}"
        return f"{self.gate} 未放行当前动作：{codes}"

    @property
    def operator_message(self) -> str:
        """Compact operator/debug summary."""
        codes = ",".join(self.finding_codes)
        return f"{self.gate}:{self.status}:{codes}".rstrip(":")

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        """Extract path-like references from structured evidence fields."""
        return tuple(_evidence_refs(self.evidence))

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
            "allow_action": self.allow_action,
            "block_task": self.block_task,
            "severity": self.severity,
            "model_message": self.model_message,
            "operator_message": self.operator_message,
            "evidence_refs": list(self.evidence_refs),
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


_SAFETY_GATES = {
    "approval_binding",
    "idempotency_ledger",
    "network_safety",
    "path_url_command",
    "skill_guard",
    "tool_effect",
    "tool_manifest",
}

_QUALITY_GATES = {
    "artifact_gate",
    "artifact_provenance",
    "artifact_report",
    "delivery_closeout",
    "delivery_quality",
    "document_content_quality",
    "fact_evidence",
    "final_closeout",
}

_SAFETY_CODE_PREFIXES = (
    "APPROVAL_",
    "DANGEROUS_",
    "IDEMPOTENCY_",
    "NETWORK_",
    "PATH_OUTSIDE_",
    "SECURITY_",
    "TOOL_NOT_",
)


def _safety_code(code: str) -> bool:
    text = str(code or "").upper()
    return any(text.startswith(prefix) for prefix in _SAFETY_CODE_PREFIXES)


def _evidence_refs(value: object) -> list[str]:
    refs: list[str] = []
    for key, item in _walk_evidence(value):
        if _ref_key(key):
            _append_ref_value(item, refs)
    return _dedupe_refs(refs)


def _walk_evidence(value: object) -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = []
    stack: list[object] = [value]
    while stack:
        _walk_evidence_item(stack.pop(), rows, stack)
    return rows


def _walk_evidence_item(item: object, rows: list[tuple[str, object]], stack: list[object]) -> None:
    if isinstance(item, dict):
        rows.extend((str(key), val) for key, val in item.items())
        stack.extend(val for val in item.values() if isinstance(val, dict | list | tuple))
    elif isinstance(item, list | tuple):
        stack.extend(item)


def _append_ref_value(value: object, refs: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            refs.append(text)
        return
    if isinstance(value, list | tuple | set):
        for item in value:
            _append_ref_value(item, refs)


def _ref_key(key: str) -> bool:
    normalized = key.lower()
    return normalized.endswith("_ref") or normalized.endswith("_refs") or normalized in {"path", "paths"}


def _dedupe_refs(refs: list[str]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        if ref not in result:
            result.append(ref)
    return result


__all__ = ["GateContext", "GateDecision", "GateFinding", "GateValidator"]
