"""task-local shared workspace facts for subagent collaboration.

Human version:
The shared workspace is a task-local coordination surface. It stores compact
status messages, findings, and evidence packet refs for sibling subagents, but
does not write subagent context into main long-term memory.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_jsonl_records,
    jsonl_lines,
    read_jsonl_objects,
    write_json_object,
    write_jsonl_records,
)
from ..common.path_segments import safe_path_segment


@dataclass(frozen=True)
class SharedWorkspaceResult:
    """Concrete files for the task-local shared collaboration surface."""

    blackboard_md: Path
    messages_jsonl: Path
    findings_jsonl: Path
    evidence_packets_dir: Path
    evidence_index_jsonl: Path


@dataclass(frozen=True)
class SyncSharedWorkspaceRequest:
    """Bundle inputs for syncing a task-local shared workspace."""

    task_workspace_root: Path
    task: Any
    now: float


@dataclass(frozen=True)
class _BlackboardWriteRequest:
    path: Path
    task: Any
    now: float
    findings: list[dict[str, object]]
    evidence_index: list[dict[str, object]]


def sync_shared_workspace(
    request: SyncSharedWorkspaceRequest | Path | None = None,
    task: Any | None = None,
    *,
    task_workspace_root: Path | None = None,
    now: float | None = None,
) -> SharedWorkspaceResult:
    """Write compact task-local shared facts for one subagent save."""

    inputs = _coerce_sync_request(
        request,
        task=task,
        task_workspace_root=task_workspace_root,
        now=now,
    )
    paths = shared_workspace_paths(inputs.task_workspace_root)
    paths.evidence_packets_dir.mkdir(parents=True, exist_ok=True)
    _append_message(paths.messages_jsonl, _message_payload(inputs.task, inputs.now))
    findings = _merge_jsonl_by_id(paths.findings_jsonl, _finding_records(inputs.task, inputs.now))
    evidence_index = _write_evidence_packets(
        paths.evidence_packets_dir,
        paths.evidence_index_jsonl,
        inputs.task,
        inputs.now,
    )
    _write_blackboard(
        _BlackboardWriteRequest(
            path=paths.blackboard_md,
            task=inputs.task,
            now=inputs.now,
            findings=findings,
            evidence_index=evidence_index,
        )
    )
    return paths


def _coerce_sync_request(
    request: SyncSharedWorkspaceRequest | Path | None,
    task: Any | None,
    *,
    task_workspace_root: Path | None,
    now: float | None,
) -> SyncSharedWorkspaceRequest:
    if isinstance(request, SyncSharedWorkspaceRequest):
        return request
    resolved_root = task_workspace_root if task_workspace_root is not None else request
    if resolved_root is None or task is None or now is None:
        raise TypeError("sync_shared_workspace requires task_workspace_root, task, and now")
    return SyncSharedWorkspaceRequest(
        task_workspace_root=Path(resolved_root),
        task=task,
        now=now,
    )


def shared_workspace_paths(task_workspace_root: Path) -> SharedWorkspaceResult:
    """Return shared workspace paths without writing them."""

    shared_dir = task_workspace_root / "shared"
    return SharedWorkspaceResult(
        blackboard_md=shared_dir / "blackboard.md",
        messages_jsonl=shared_dir / "messages.jsonl",
        findings_jsonl=shared_dir / "findings.jsonl",
        evidence_packets_dir=shared_dir / "evidence_packets",
        evidence_index_jsonl=shared_dir / "evidence_packets" / "index.jsonl",
    )


def _message_payload(task: Any, now: float) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "version": 1,
        "message_id": f"msg-{safe_path_segment(run_id, default='item', replacement='_')}-{int(now * 1000)}",
        "message_type": "status_update",
        "task_id": task_id,
        "run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "current_step": str(getattr(task, "current_step", "")),
        "summary": _summary(task),
        "blockers": list(getattr(task, "blockers", []) or []),
        "evidence_packet_count": len(list(getattr(task, "evidence_packets", []) or [])),
        "finding_count": len(list(getattr(task, "findings", []) or [])),
        "created_at": _utc_iso(now),
    }


def _finding_records(task: Any, now: float) -> list[dict[str, object]]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    records: list[dict[str, object]] = []
    for index, item in enumerate(list(getattr(task, "findings", []) or []), start=1):
        payload = _record_payload(item)
        if not payload:
            continue
        payload.update({"version": 1, "task_id": task_id, "run_id": run_id, "source": "subagent_findings"})
        payload.setdefault("id", f"finding-{safe_path_segment(run_id, default='item', replacement='_')}-{index}")
        payload.setdefault("created_at", now)
        records.append(payload)
    return records


def _write_evidence_packets(index_dir: Path, index_path: Path, task: Any, now: float) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    for index, item in enumerate(list(getattr(task, "evidence_packets", []) or []), start=1):
        payload = _record_payload(item)
        if not payload:
            continue
        packet_id = str(payload.get("id") or f"evidence-{safe_path_segment(run_id, default='item', replacement='_')}-{index}")
        payload.update({"version": 1, "id": packet_id, "task_id": task_id, "run_id": run_id})
        packet_path = index_dir / f"{safe_path_segment(packet_id, default='item', replacement='_')}.json"
        write_json_object(packet_path, payload, sort_keys=False)
        records.append(_evidence_index_record(payload, packet_path, now))
    return _merge_jsonl_by_id(index_path, records)


def _evidence_index_record(payload: dict[str, object], packet_path: Path, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "id": str(payload.get("id") or ""),
        "task_id": str(payload.get("task_id") or ""),
        "run_id": str(payload.get("run_id") or ""),
        "claim": str(payload.get("claim") or ""),
        "path": str(packet_path),
        "evidence_refs": list(payload.get("evidence_refs") or []),
        "artifact_refs": list(payload.get("artifact_refs") or []),
        "confidence": float(payload.get("confidence") or 0.0),
        "updated_at": now,
    }


def _write_blackboard(request: _BlackboardWriteRequest) -> None:
    task = request.task
    blockers = "\n".join(f"- {item}" for item in list(getattr(task, "blockers", []) or [])) or "- 暂无"
    finding_lines = "\n".join(f"- {_claim_text(item)}" for item in request.findings) or "- 暂无"
    evidence_lines = "\n".join(f"- {_claim_text(item)}" for item in request.evidence_index) or "- 暂无"
    content = (
        "# Blackboard\n\n"
        f"- task_id: {getattr(task, 'root_id', '') or getattr(task, 'id', '')}\n"
        f"- last_run_id: {getattr(task, 'id', '')}\n"
        f"- status: {getattr(task, 'status', '')}\n"
        f"- current_step: {getattr(task, 'current_step', '') or getattr(task, 'status', '')}\n"
        f"- updated_at: {_utc_iso(request.now)}\n\n"
        "## Latest Summary\n\n"
        f"{_summary(task) or '暂无'}\n\n"
        "## Blockers\n\n"
        f"{blockers}\n\n"
        "## Findings\n\n"
        f"{finding_lines}\n\n"
        "## Evidence Packets\n\n"
        f"{evidence_lines}\n"
    )
    _write_text(request.path, content)


def _record_payload(item: object) -> dict[str, object]:
    if is_dataclass(item):
        return asdict(item)
    return dict(item) if isinstance(item, dict) else {}


def _claim_text(item: object) -> str:
    payload = _record_payload(item)
    return str(payload.get("claim") or payload.get("id") or "未命名")


def _summary(task: Any) -> str:
    return str(getattr(task, "latest_summary", "") or getattr(task, "current_step", "") or getattr(task, "status", ""))[:500]


def _append_message(path: Path, payload: dict[str, object]) -> None:
    if _last_message_signature(path) == _message_signature(payload):
        return
    append_jsonl_records(path, [payload])


def _last_message_signature(path: Path) -> tuple[object, ...] | None:
    if not path.exists():
        return None
    try:
        # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
        lines = jsonl_lines(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return _message_signature(payload) if isinstance(payload, dict) else None
    return None


def _message_signature(payload: dict[str, object]) -> tuple[object, ...]:
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    return (
        payload.get("message_type"),
        payload.get("task_id"),
        payload.get("run_id"),
        payload.get("status"),
        payload.get("current_step"),
        payload.get("summary"),
        tuple(str(item) for item in blockers),
        payload.get("finding_count"),
        payload.get("evidence_packet_count"),
    )


def _merge_jsonl_by_id(path: Path, records: list[dict[str, object]]) -> list[dict[str, object]]:
    merged = read_jsonl_objects(path)
    positions = {str(item.get("id") or ""): index for index, item in enumerate(merged) if item.get("id")}
    for record in records:
        _merge_record_by_id(merged, positions, record)
    write_jsonl_records(path, merged)
    return merged


def _merge_record_by_id(
    merged: list[dict[str, object]],
    positions: dict[str, int],
    record: dict[str, object],
) -> None:
    record_id = str(record.get("id") or "")
    if record_id and record_id in positions:
        merged[positions[record_id]] = record
        return
    if record_id:
        positions[record_id] = len(merged)
    merged.append(record)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "SharedWorkspaceResult",
    "SyncSharedWorkspaceRequest",
    "shared_workspace_paths",
    "sync_shared_workspace",
]
