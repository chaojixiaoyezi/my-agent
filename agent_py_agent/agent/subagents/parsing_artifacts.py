# LLM: Artifact parsing aliases live outside parsing.py so the main result parser stays thin.
# 模块用途: 统一处理 runner 产物字段别名，把模型常写的同义字段收敛为标准 artifacts。

from __future__ import annotations

from .parsing_values import _dict_list


# LLM: artifact_items_from_payload is the only schema-tolerance point for runner product refs.
# 函数用途: 把模型常写的 deliverables/output_files/files 同义字段收敛成标准 artifacts，避免验收链路漏测真实产物。
def artifact_items_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    """Read artifact refs from the canonical field plus safe compatibility aliases."""

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for field in ("artifacts", "deliverables", "output_files", "files"):
        result.extend(_new_artifact_items(payload.get(field, []), seen))
    for field in ("files_modified", "modified_files", "changed_files", "created_files"):
        result.extend(_artifact_items_from_string_refs(payload.get(field, []), seen))
    # LLM: top-level artifact_refs is a product-ref alias from repair/validator agents, not only evidence metadata.
    # 函数用途: 模型把产物路径直接放到 artifact_refs 时，也要进入标准 artifacts，避免父级验收漏查真实文件。
    result.extend(_artifact_items_from_string_refs(payload.get("artifact_refs", []), seen, summary="reported artifact ref"))
    result.extend(_artifact_items_from_evidence(payload.get("evidence", []), seen))
    result.extend(_artifact_items_from_evidence_packets(payload.get("evidence_packets", []), seen))
    return result


# LLM: _new_artifact_items filters one alias field without expanding parser nesting.
# 函数用途: 从单个字段里取出未见过的产物条目，只接受能定位文件的 path/id/artifact_id。
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


# LLM: _artifact_items_from_evidence recovers product refs that structured repair put under evidence.
# 函数用途: 从 evidence 里的 artifact/path 证据补回 artifacts，避免修复器把产物路径放错字段后父级漏测。
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


# LLM: _artifact_items_from_evidence_packets treats explicit artifact_refs as product refs, not prose.
# 函数用途: 从 evidence_packets.artifact_refs 字符串补 artifacts，保持验收链路 refs-first。
def _artifact_items_from_evidence_packets(value: object, seen: set[str]) -> list[dict[str, object]]:
    """Return artifact refs listed in evidence packets."""

    items: list[dict[str, object]] = []
    for packet in _dict_list(value):
        items.extend(_new_packet_artifact_items(packet, seen))
    return items


# LLM: _artifact_items_from_string_refs recovers common repair-output file lists as product artifacts.
# 函数用途: 把 files_modified/created_files 这类字符串路径列表转成标准 artifact 条目，让父级验收能继续跑机器检查。
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


# LLM: _new_packet_artifact_items keeps packet alias recovery shallow for code-size guards.
# 函数用途: 从单个 evidence packet 取未见过的 artifact_refs，并生成标准 artifact 条目。
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


# LLM: _string_refs accepts only non-empty string refs from evidence packet arrays.
# 函数用途: 提取 evidence packet 中的字符串 artifact refs，忽略对象/空值以避免误判。
def _string_refs(value: object) -> list[str]:
    """Normalize evidence packet refs to non-empty strings."""

    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


# LLM: _artifact_key keeps broad alias support from treating arbitrary lists as files.
# 函数用途: 只用 path/artifact_id/id 生成去重键，避免把普通说明列表误导入 artifacts。
def _artifact_key(item: dict[str, object]) -> str:
    """Return the stable identity for a product artifact ref."""

    for key in ("path", "artifact_id", "id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""
