from __future__ import annotations

"""LLM: task-local shared workspace facts for subagent collaboration.

Human version:
The shared workspace is a task-local coordination surface. It stores compact
status messages, findings, and evidence packet refs for sibling subagents, but
does not write subagent context into main long-term memory.
"""

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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

    # LLM: shared workspace inputs stay task-local in one bundle and never imply main memory writes.
    task_workspace_root: Path
    task: Any
    now: float


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
    _write_blackboard(paths.blackboard_md, inputs.task, inputs.now)
    _append_message(paths.messages_jsonl, _message_payload(inputs.task, inputs.now))
    _write_jsonl(paths.findings_jsonl, _finding_records(inputs.task, inputs.now))
    _write_evidence_packets(
        paths.evidence_packets_dir,
        paths.evidence_index_jsonl,
        inputs.task,
        inputs.now,
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
        "message_id": f"msg-{_safe_segment(run_id)}-{int(now * 1000)}",
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
        payload.setdefault("id", f"finding-{_safe_segment(run_id)}-{index}")
        payload.setdefault("created_at", now)
        records.append(payload)
    return records


def _write_evidence_packets(index_dir: Path, index_path: Path, task: Any, now: float) -> None:
    records: list[dict[str, object]] = []
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    for index, item in enumerate(list(getattr(task, "evidence_packets", []) or []), start=1):
        payload = _record_payload(item)
        if not payload:
            continue
        packet_id = str(payload.get("id") or f"evidence-{_safe_segment(run_id)}-{index}")
        payload.update({"version": 1, "id": packet_id, "task_id": task_id, "run_id": run_id})
        packet_path = index_dir / f"{_safe_segment(packet_id)}.json"
        _write_json(packet_path, payload)
        records.append(_evidence_index_record(payload, packet_path, now))
    _write_jsonl(index_path, records)


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


def _write_blackboard(path: Path, task: Any, now: float) -> None:
    blockers = "\n".join(f"- {item}" for item in list(getattr(task, "blockers", []) or [])) or "- 暂无"
    findings = "\n".join(f"- {_claim_text(item)}" for item in list(getattr(task, "findings", []) or [])) or "- 暂无"
    evidence = "\n".join(f"- {_claim_text(item)}" for item in list(getattr(task, "evidence_packets", []) or [])) or "- 暂无"
    content = (
        "# Blackboard\n\n"
        f"- task_id: {getattr(task, 'root_id', '') or getattr(task, 'id', '')}\n"
        f"- last_run_id: {getattr(task, 'id', '')}\n"
        f"- status: {getattr(task, 'status', '')}\n"
        f"- current_step: {getattr(task, 'current_step', '') or getattr(task, 'status', '')}\n"
        f"- updated_at: {_utc_iso(now)}\n\n"
        "## Latest Summary\n\n"
        f"{_summary(task) or '暂无'}\n\n"
        "## Blockers\n\n"
        f"{blockers}\n\n"
        "## Findings\n\n"
        f"{findings}\n\n"
        "## Evidence Packets\n\n"
        f"{evidence}\n"
    )
    _write_text(path, content)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _safe_segment(value: str) -> str:
    return str(value or "item").replace("/", "_").replace("\\", "_").strip() or "item"


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "SharedWorkspaceResult",
    "SyncSharedWorkspaceRequest",
    "shared_workspace_paths",
    "sync_shared_workspace",
]
