# LLM: Collection mapping checks verify source items appear in final artifacts.
# 模块用途: 校验 source JSON 中的结构化条目是否能按 key_fields 映射到最终产物文本，避免“源数据有了但成品漏项”。

from __future__ import annotations

import json
from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding
from .artifact_structured_contracts import positive_int, string_list


def mapping_findings(
    items: list[object],
    contract: dict[str, object],
    source_ref: str,
    workspace_root: Path,
) -> list[ArtifactFinding]:
    mapping = contract.get("mapping")
    if not isinstance(mapping, dict):
        return []
    artifact_ref = str(mapping.get("artifact_ref") or "").strip()
    key_fields = string_list(mapping.get("key_fields"))
    if not artifact_ref or not key_fields:
        return [_finding("ARTIFACT_MAPPING_CONTRACT_INVALID", "mapping requires artifact_ref and key_fields.", source_ref)]
    artifact_path = _workspace_path(artifact_ref, workspace_root)
    if not _inside_workspace(artifact_path, workspace_root):
        return [_finding("ARTIFACT_MAPPING_OUTSIDE_WORKSPACE", "mapping artifact is outside workspace_root.", artifact_ref)]
    try:
        text = artifact_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [_finding("ARTIFACT_MAPPING_TARGET_MISSING", "mapping artifact does not exist.", artifact_ref)]
    mapped_items = [item for item in items if isinstance(item, dict) and _item_mapped(item, key_fields, text)]
    required = positive_int(mapping.get("min_mapped_items")) or len(items)
    if len(mapped_items) >= required:
        return []
    missing_keys = [
        _item_key_values(item, key_fields)
        for item in items
        if isinstance(item, dict) and not _item_mapped(item, key_fields, text)
    ]
    return [
        _finding(
            "ARTIFACT_MAPPING_MISSING",
            f"artifact maps {len(mapped_items)} source items, expected at least {required}.",
            artifact_ref,
            _compact_json({"mapped": len(mapped_items), "required": required, "missing_keys": missing_keys[:10]}),
        )
    ]


def _item_mapped(item: dict[str, object], key_fields: list[str], text: str) -> bool:
    keys = [str(item.get(field) or "").strip() for field in key_fields]
    return bool(keys) and all(key and key in text for key in keys)


def _item_key_values(item: dict[str, object], key_fields: list[str]) -> dict[str, str]:
    return {field: str(item.get(field) or "").strip() for field in key_fields if str(item.get(field) or "").strip()}


def _workspace_path(ref: str, workspace_root: Path) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else (workspace_root / path).resolve()


def _inside_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(workspace_root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _finding(code: str, message: str, location: str = "", value: str = "") -> ArtifactFinding:
    return ArtifactFinding(code=code, severity="hard", message=message, location=location, value=value)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["mapping_findings"]
