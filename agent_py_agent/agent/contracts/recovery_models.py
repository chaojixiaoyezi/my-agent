# LLM: Recovery models are the shared serialized shape for contract repair feedback.
# 模块用途: 定义合同返工包和构建请求；机器读取状态/动作，中文字段只给模型和用户看。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RecoveryEnvelopeRequest:
    gate: str
    status: str
    allowed: bool
    findings: tuple[dict[str, Any], ...] | list[dict[str, Any]]
    recommended_action: str = ""
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class RecoveryEnvelope:
    status: str
    can_auto_repair: bool
    requires_user: bool
    terminal: bool
    next_status: str
    recommended_action: str
    finding_codes: tuple[str, ...]
    actions: tuple[dict[str, Any], ...] = ()
    message_zh: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "can_auto_repair": self.can_auto_repair,
            "requires_user": self.requires_user,
            "terminal": self.terminal,
            "next_status": self.next_status,
            "recommended_action": self.recommended_action,
            "finding_codes": list(self.finding_codes),
            "actions": [dict(item) for item in self.actions],
            "message_zh": self.message_zh,
            "evidence": dict(self.evidence),
        }


__all__ = ["RecoveryEnvelope", "RecoveryEnvelopeRequest"]
