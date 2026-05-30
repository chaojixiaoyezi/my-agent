# LLM: Collaboration package exposes the generic multi-agent coordination control plane.
# 模块用途: 对外导出协作 case、能力、请求、证据和 coordinator，不承载任何业务专项逻辑。

from __future__ import annotations

from .coordinator import CollaborationCoordinator, CollaborationCoordinatorPolicy
from .models import (
    AgentCapability,
    CaseDecision,
    CaseParticipant,
    CollaborationCase,
    CollaborationRequest,
    EvidencePacket,
)
from .store import CollaborationStore
from .tools import (
    InspectCollaborationTool,
    RaiseCollaborationTool,
    SubmitCollaborationResultTool,
    UpdateCollaborationTool,
)

__all__ = [
    "AgentCapability",
    "CaseDecision",
    "CaseParticipant",
    "CollaborationCase",
    "CollaborationCoordinator",
    "CollaborationCoordinatorPolicy",
    "CollaborationRequest",
    "CollaborationStore",
    "EvidencePacket",
    "InspectCollaborationTool",
    "RaiseCollaborationTool",
    "SubmitCollaborationResultTool",
    "UpdateCollaborationTool",
]
