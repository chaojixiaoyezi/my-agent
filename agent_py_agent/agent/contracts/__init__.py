# LLM: Shared agent contracts keep errors, states, idempotency, and E2E scenarios reusable.
# 模块用途: 提供主代理和后续子代理共用的底层合同，不把运行规则散落成 prompt guard。

from __future__ import annotations

from .acceptance_contract import (
    AcceptanceContract,
    AcceptanceInput,
    AcceptanceResult,
    evaluate_acceptance_contract,
)
from .activity_timeout import (
    ActivitySnapshot,
    ActivityTimeoutDecision,
    ActivityTimeoutPolicy,
    decide_activity_timeout,
)
from .evidence_contract import (
    EvidenceClaim,
    EvidenceContractReport,
    EvidenceContractRequest,
    EvidenceSourceRef,
    evaluate_evidence_contract,
)

__all__ = [
    "AcceptanceContract",
    "AcceptanceInput",
    "AcceptanceResult",
    "ActivitySnapshot",
    "ActivityTimeoutDecision",
    "ActivityTimeoutPolicy",
    "EvidenceClaim",
    "EvidenceContractReport",
    "EvidenceContractRequest",
    "EvidenceSourceRef",
    "decide_activity_timeout",
    "evaluate_acceptance_contract",
    "evaluate_evidence_contract",
]
