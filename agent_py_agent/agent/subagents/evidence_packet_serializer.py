# LLM: Evidence packets use an explicit whitelist, never asdict/vars or automatic expansion of future sensitive fields.
# 模块用途: 为结构化结果和产物兜底证据提供一致载荷，不写文件或改变任务状态。
from __future__ import annotations

from .models import EvidencePacket


# LLM: Return a new dictionary of historical fields while retaining values and list identity without content transformation.
# 函数用途: 只序列化获准的证据包字段，不输出未知属性。
def serialize_evidence_packet(packet: EvidencePacket) -> dict[str, object]:
    return {
        "id": packet.id,
        "claim": packet.claim,
        "checked_scope": packet.checked_scope,
        "evidence_refs": packet.evidence_refs,
        "artifact_refs": packet.artifact_refs,
        "counter_evidence_refs": packet.counter_evidence_refs,
        "confidence": packet.confidence,
        "unresolved_risks": packet.unresolved_risks,
        "created_at": packet.created_at,
    }
