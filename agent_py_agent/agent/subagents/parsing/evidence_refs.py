
from __future__ import annotations

from ...common.value_parsing import text_or_sequence_strings
from .values import _dict_list


def evidence_packets_with_top_level_refs(payload: dict[str, object]) -> list[dict[str, object]]:
    packets = _dict_list(payload.get("evidence_packets", []))
    evidence_refs = text_or_sequence_strings(payload.get("evidence_refs", []))
    artifact_refs = text_or_sequence_strings(payload.get("artifact_refs", []))
    if not evidence_refs and not artifact_refs:
        return packets
    packet_ref_keys = _packet_ref_keys(packets)
    missing_evidence = [ref for ref in evidence_refs if ("evidence", ref) not in packet_ref_keys]
    missing_artifacts = [ref for ref in artifact_refs if ("artifact", ref) not in packet_ref_keys]
    if not missing_evidence and not missing_artifacts:
        return packets
    packets.append(_top_level_refs_packet(payload, missing_evidence, missing_artifacts))
    return packets


def _top_level_refs_packet(
    payload: dict[str, object],
    missing_evidence: list[str],
    missing_artifacts: list[str],
) -> dict[str, object]:
    return {
        "id": _top_level_refs_packet_id(payload),
        "claim": "runner supplied top-level traceable refs",
        "checked_scope": "runner_top_level_refs",
        "evidence_refs": missing_evidence,
        "artifact_refs": missing_artifacts,
        "confidence": 0.6,
    }


def _packet_ref_keys(packets: list[dict[str, object]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for packet in packets:
        for ref in text_or_sequence_strings(packet.get("evidence_refs", [])):
            keys.add(("evidence", ref))
        for ref in text_or_sequence_strings(packet.get("artifact_refs", [])):
            keys.add(("artifact", ref))
    return keys


def _top_level_refs_packet_id(payload: dict[str, object]) -> str:
    raw = str(payload.get("run_id") or "runner-output").strip() or "runner-output"
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw)
    return f"evpkt-top-level-refs-{safe}"[:120]
