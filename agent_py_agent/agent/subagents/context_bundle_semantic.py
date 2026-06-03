
from __future__ import annotations

from typing import Any


def semantic_context_missing_fields(bundle: Any) -> list[str]:
    expected = _bundle_required_file_terms(bundle)
    if not expected:
        return []
    output_contract = getattr(bundle, "output_contract", {})
    output_required = set(_object_string_list(output_contract.get("required_files")))
    packet = getattr(bundle, "task_packet", {})
    packet = packet if isinstance(packet, dict) else {}
    file_contract = packet.get("file_contract") if isinstance(packet.get("file_contract"), dict) else {}
    packet_required = set(_object_string_list(file_contract.get("required_files")))
    missing: list[str] = []
    for filename in expected:
        if filename not in output_required:
            missing.append(f"output_contract.required_files:{filename}")
        if filename not in packet_required:
            missing.append(f"task_packet.file_contract.required_files:{filename}")
    return missing


def _bundle_required_file_terms(bundle: Any) -> list[str]:
    reserved = getattr(bundle, "reserved", {})
    if not isinstance(reserved, dict):
        return []
    return _dedupe_file_terms(_object_string_list(reserved.get("expected_required_files")))


def _dedupe_file_terms(values) -> list[str]:
    terms: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in terms:
            terms.append(text)
    return terms


def _object_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item or "").strip())]
