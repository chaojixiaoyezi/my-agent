# LLM: Artifact-only runner output needs a refs-only evidence bridge for parent acceptance.
# 模块用途: 从 runner artifacts 生成和合并证据引用；不读取 artifact 正文，只维护可追踪 refs。

from __future__ import annotations

from .models import EvidencePacket, SubAgentTask
from .utils import _merge_list, _new_id


# LLM: artifact_ref extracts artifact pointers without expanding large files.
# 函数用途: 从 artifact metadata 读取 path/uri/artifact_id；保持 refs-only，供证据合成和 artifact_refs 合并使用。
def artifact_ref(item: dict[str, object]) -> str:
    return str(item.get("path") or item.get("uri") or item.get("artifact_id") or "").strip()


# LLM: _artifact_claim keeps synthesized evidence concise for parent/verifier checks.
# 函数用途: 给系统补齐的 artifact evidence packet 生成 claim；优先使用模型提供的 summary。
def _artifact_claim(item: dict[str, object], ref: str) -> str:
    summary = str(item.get("summary") or "").strip()
    if summary:
        return f"artifact produced: {summary}"
    kind = str(item.get("kind") or "artifact").strip() or "artifact"
    return f"{kind} artifact produced: {ref}"


# LLM: _packet_payload mirrors the structured output evidence packet JSON shape.
# 函数用途: 把 EvidencePacket 转成 output.json 里的稳定字典格式，保持字段完整。
def _packet_payload(packet: EvidencePacket) -> dict[str, object]:
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


# LLM: synthesize_artifact_evidence_packets protects artifact-only successful work from false rejection.
# 函数用途: runner 已声明 artifacts 但漏写 evidence_packets 时，生成低置信度 refs-only 证据包并挂到 task。
def synthesize_artifact_evidence_packets(
    task: SubAgentTask,
    artifacts: list[dict[str, object]],
    now: float,
) -> list[dict[str, object]]:
    packets: list[dict[str, object]] = []
    for item in artifacts:
        ref = artifact_ref(item)
        if not ref:
            continue
        packet = EvidencePacket(
            id=_new_id("evpkt"),
            claim=_artifact_claim(item, ref),
            checked_scope="runner_artifacts",
            artifact_refs=[ref],
            confidence=0.5,
            created_at=now,
        )
        task.evidence_packets.append(packet)
        task.artifact_refs = _merge_list(task.artifact_refs, packet.artifact_refs)
        packets.append(_packet_payload(packet))
    return packets


# LLM: merge_artifact_evidence keeps artifact refs and synthesized packets in one small service boundary.
# 函数用途: 合并 artifact_refs；如果没有显式 evidence_packets，则从 artifacts 补齐 refs-only 证据包。
def merge_artifact_evidence(
    task: SubAgentTask,
    artifacts: list[dict[str, object]],
    evidence_packets: list[dict[str, object]],
    now: float,
) -> list[dict[str, object]]:
    if not evidence_packets:
        evidence_packets = synthesize_artifact_evidence_packets(task, artifacts, now)
    task.artifact_refs = _merge_list(task.artifact_refs, [ref for ref in (artifact_ref(item) for item in artifacts) if ref])
    return evidence_packets
