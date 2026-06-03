
from __future__ import annotations

from .values import _dict_list


def artifact_items_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    """Read artifact refs from the canonical field plus safe compatibility aliases."""

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for field in ("artifacts", "deliverables", "output_files", "files"):
        result.extend(_new_artifact_items(payload.get(field, []), seen))
    for field in ("files_modified", "modified_files", "changed_files", "created_files"):
        result.extend(_artifact_items_from_string_refs(payload.get(field, []), seen))
    result.extend(_artifact_items_from_string_refs(payload.get("artifact_refs", []), seen, summary="reported artifact ref"))
    result.extend(_top_level_file_path_artifacts(payload, seen))
    result.extend(_artifact_items_from_evidence(payload.get("evidence", []), seen))
    result.extend(_artifact_items_from_evidence_packets(payload.get("evidence_packets", []), seen))
    return result


def _new_artifact_items(value: object, seen: set[str]) -> list[dict[str, object]]:
    """Return unseen artifact-like entries from one loose model field."""

    items: list[dict[str, object]] = []
    for item in _dict_list(value):
        key = _artifact_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _artifact_items_from_evidence(value: object, seen: set[str]) -> list[dict[str, object]]:
    """Return artifact refs declared as evidence items."""

    items: list[dict[str, object]] = []
    for item in _dict_list(value):
        if str(item.get("kind") or "").strip().lower() != "artifact":
            continue
        path = str(item.get("path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        items.append({"path": path, "kind": "file", "summary": str(item.get("summary") or "")})
    return items


def _artifact_items_from_evidence_packets(value: object, seen: set[str]) -> list[dict[str, object]]:
    """Return artifact refs listed in evidence packets."""

    items: list[dict[str, object]] = []
    for packet in _dict_list(value):
        items.extend(_new_packet_artifact_items(packet, seen))
    return items


def _artifact_items_from_string_refs(
    value: object,
    seen: set[str],
    *,
    summary: str = "reported modified artifact",
) -> list[dict[str, object]]:
    """Return artifact items from string path lists emitted by repair workers."""

    items: list[dict[str, object]] = []
    for ref in _string_refs(value):
        if ref in seen:
            continue
        seen.add(ref)
        items.append({"path": ref, "kind": "file", "summary": summary})
    return items


def _top_level_file_path_artifacts(payload: dict[str, object], seen: set[str]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for key in ("file_path", "output_path", "path"):
        ref = str(payload.get(key) or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        items.append({"path": ref, "kind": "file", "summary": f"reported {key} artifact"})
    return items


def _new_packet_artifact_items(packet: dict[str, object], seen: set[str]) -> list[dict[str, object]]:
    """Return unseen artifact items from one evidence packet."""

    summary = str(packet.get("claim") or "")
    items: list[dict[str, object]] = []
    for ref in _string_refs(packet.get("artifact_refs", [])):
        if ref in seen:
            continue
        seen.add(ref)
        items.append({"path": ref, "kind": "file", "summary": summary})
    return items


def _string_refs(value: object) -> list[str]:
    """Normalize evidence packet refs to non-empty strings."""

    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _artifact_key(item: dict[str, object]) -> str:
    """Return the stable identity for a product artifact ref."""

    for key in ("path", "artifact_id", "id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""
