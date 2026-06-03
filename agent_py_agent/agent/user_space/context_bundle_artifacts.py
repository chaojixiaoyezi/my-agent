
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import JsonObjectReadReport, read_json_object_report
from ..memory_archive.compact_tool_output_refs import tool_output_source_refs


@dataclass(frozen=True)
class MainContextBundleArtifactUpdateRequest:
    context_bundle_path: str
    workspace_root: str | Path
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""


def update_main_context_bundle_artifacts(request: MainContextBundleArtifactUpdateRequest) -> dict[str, Any]:
    path = Path(request.context_bundle_path)
    bundle_report = _read_bundle_report(path)
    payload = bundle_report.payload
    if not payload:
        result: dict[str, Any] = {"ok": False, "status": "missing_or_invalid_context_bundle", "artifact_count": 0}
        if bundle_report.load_error:
            result["load_error"] = bundle_report.load_error
        return result
    scope = _scope(request)
    refs = tool_output_source_refs(request.workspace_root, scope) if _has_scope(scope) else []
    if not refs:
        return {"ok": True, "status": "no_matching_tool_output_artifacts", "artifact_count": 0}
    payload["artifact_refs"] = _merged_artifact_refs(payload.get("artifact_refs"), refs)
    _write_bundle(path, payload)
    _rewrite_latest_if_needed(path)
    return {"ok": True, "status": "updated", "artifact_count": len(refs)}


def _merged_artifact_refs(current: object, refs: list[dict[str, Any]]) -> dict[str, Any]:
    payload = current if isinstance(current, dict) else {}
    items = [item for item in payload.get("items", []) if isinstance(item, dict)]
    seen = {str(item.get("ref") or item.get("path") or "") for item in items}
    for ref in refs:
        path = str(ref.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        items.append({
            "ref": path,
            "kind": "tool_output",
            "source": "tool_output_index",
            "tool": str(ref.get("tool") or ""),
            "call_id": str(ref.get("call_id") or ""),
            "scoped_call_id": str(ref.get("scoped_call_id") or ""),
            "sha256": str(ref.get("sha256") or ""),
            "size_bytes": int(ref.get("size_bytes", 0) or 0),
            "reserved": {},
        })
    return {
        "items": items,
        "collection_phase": "post_tool_loop",
        "body_policy": "refs_only_read_explicitly",
        "reserved": dict(payload.get("reserved", {}) if isinstance(payload.get("reserved"), dict) else {}),
    }


def _scope(request: MainContextBundleArtifactUpdateRequest) -> dict[str, str]:
    return {
        "request_id": str(request.request_id or "").strip(),
        "run_id": str(request.run_id or "").strip(),
        "task_id": str(request.task_id or "").strip(),
    }


def _has_scope(scope: dict[str, str]) -> bool:
    return any(scope.values())


def _read_bundle(path: Path) -> dict[str, Any]:
    return _read_bundle_report(path).payload


def _read_bundle_report(path: Path) -> JsonObjectReadReport:
    return read_json_object_report(path, context="context_bundle_artifacts.context_bundle")


def _write_bundle(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _rewrite_latest_if_needed(path: Path) -> None:
    latest = path.parent / "latest_context_bundle.json"
    if latest.exists():
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


__all__ = ["MainContextBundleArtifactUpdateRequest", "update_main_context_bundle_artifacts"]
