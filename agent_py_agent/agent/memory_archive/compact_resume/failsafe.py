from __future__ import annotations

"""refs-only fail-safe checkpoint extraction for compact resume."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...runtime_errors import runtime_error_report


@dataclass(frozen=True)
class FailSafeCheckpointReport:
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    load_errors: list[dict[str, Any]] = field(default_factory=list)


def collect_fail_safe_checkpoints(restore_refs: dict[str, Any]) -> list[dict[str, Any]]:
    return collect_fail_safe_checkpoint_report(restore_refs).checkpoints


def collect_fail_safe_checkpoint_report(restore_refs: dict[str, Any]) -> FailSafeCheckpointReport:
    checkpoints: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs, dict) else {}
    archive_files = source_refs.get("archive_files", []) if isinstance(source_refs, dict) else []
    for item in archive_files if isinstance(archive_files, list) else []:
        path = Path(str(item.get("path", "") or ""))
        if path.name.endswith(".jsonl"):
            report = _checkpoint_lines_from_path(path)
            checkpoints.extend(report.checkpoints)
            load_errors.extend(report.load_errors)
    return FailSafeCheckpointReport(
        checkpoints=_dedupe_checkpoints(checkpoints),
        load_errors=load_errors,
    )


def _checkpoint_lines_from_path(path: Path) -> FailSafeCheckpointReport:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return FailSafeCheckpointReport(load_errors=[_load_error(path, exc)])
    checkpoints: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            load_errors.append(_load_error(path, exc, line_no=line_no))
            continue
        if isinstance(payload, dict) and _is_tool_output_fail_safe(payload):
            checkpoints.append(_checkpoint_payload(path, line_no, payload))
    return FailSafeCheckpointReport(checkpoints=checkpoints, load_errors=load_errors)


def _load_error(path: Path, exc: BaseException, *, line_no: int = 0) -> dict[str, Any]:
    report = runtime_error_report(exc, context="compact_resume.fail_safe_checkpoint")
    report["path"] = str(path)
    if line_no:
        report["line_no"] = line_no
    return report


def _is_tool_output_fail_safe(payload: dict[str, Any]) -> bool:
    turn_range = payload.get("turn_range", {}) if isinstance(payload.get("turn_range"), dict) else {}
    source = str(payload.get("source") or turn_range.get("source") or "")
    return source == "tool_output_externalizer" and isinstance(payload.get("tool_calls"), list)


def _checkpoint_payload(path: Path, line_no: int, payload: dict[str, Any]) -> dict[str, Any]:
    turn_range = payload.get("turn_range", {}) if isinstance(payload.get("turn_range"), dict) else {}
    return {
        "path": str(path),
        "line_no": line_no,
        "snapshot_id": str(payload.get("snapshot_id", "") or ""),
        "source": str(payload.get("source") or turn_range.get("source") or ""),
        "status": str(payload.get("status", "") or ""),
        "request_id": str(payload.get("request_id") or turn_range.get("request_id") or ""),
        "run_id": str(payload.get("run_id") or turn_range.get("run_id") or ""),
        "task_id": str(payload.get("task_id") or turn_range.get("task_id") or ""),
        "tool_calls": _tool_output_refs(payload.get("tool_calls")),
        "next_actions": list(payload.get("next_actions", [])) if isinstance(payload.get("next_actions"), list) else [],
        "reads_artifact_bodies": False,
    }


def _tool_output_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            refs.append({
                "tool": str(item.get("tool") or item.get("name") or ""),
                "id": str(item.get("id") or item.get("tool_call_id") or ""),
                "ok": item.get("ok"),
                "output_hash": str(item.get("output_hash", "") or ""),
                "output_size_bytes": int(item.get("output_size_bytes", 0) or 0),
                "output_externalized": str(item.get("output_externalized", "") or ""),
            })
    return refs


def _dedupe_checkpoints(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        key = (str(item.get("path", "")), int(item.get("line_no", 0) or 0))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
