
"""Normalize subagent artifact refs onto the current task-local registry.

This module is deliberately read/registration oriented: it resolves refs that a
subagent reported, registers existing local artifacts, and preserves unresolved
text as recovery hints. It must not turn a missing artifact into a task failure;
closeout and the parent model decide what to do with incomplete delivery.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..artifacts.registry import ArtifactRegistration, register_artifact
from ..runtime_errors import runtime_error_report
from .models import EvidencePacket, SubAgentTask
from .result_artifact_roots import (
    artifact_candidate_roots,
    artifact_suffix_roots,
    declared_workspace_roots,
)
from .services.agent_run_state import read_agent_state_payload
from .utils import _merge_list, _new_id


def artifact_ref(item: dict[str, object]) -> str:
    return str(item.get("path") or "").strip()


def normalize_artifact_items(task: SubAgentTask, artifacts: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for item in artifacts:
        copied = _normalized_artifact_item(task, item)
        if copied is not None:
            copied = _with_registry_ref(task, copied)
            normalized.append(copied)
    return normalized


def _normalized_artifact_item(task: SubAgentTask, item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    copied = dict(item)
    resolved = normalize_artifact_ref(task, copied.get("path"))
    if resolved:
        copied["path"] = resolved
    return copied


def _with_registry_ref(task: SubAgentTask, item: dict[str, object]) -> dict[str, object]:
    ref = artifact_ref(item)
    path = _existing_local_path(ref)
    root = _registry_workspace_root(task, path)
    if path is None or root is None:
        return item
    registered = register_artifact(
        ArtifactRegistration(
            workspace_root=root,
            path=path,
            artifact_id=str(item.get("artifact_id") or ""),
            run_id=str(getattr(task, "id", "") or ""),
            task_id=str(getattr(task, "root_id", "") or getattr(task, "id", "") or ""),
            agent_id=str(getattr(task, "id", "") or ""),
            kind=str(item.get("kind") or ""),
            source="subagent_result",
            created_by_tool="subagent_runner",
        )
    )
    _append_task_registry_ref(task, registered.to_dict())
    return {**item, "artifact_id": registered.artifact_id, "registry_ref": registered.to_dict()}


def _append_task_registry_ref(task: SubAgentTask, record: dict[str, object]) -> None:
    raw_attrs = getattr(task, "attributes", {})
    attrs = dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
    refs = attrs.get("artifact_registry_refs")
    rows = [item for item in refs if isinstance(item, dict)] if isinstance(refs, list) else []
    artifact_id = str(record.get("artifact_id") or "")
    rows = [item for item in rows if str(item.get("artifact_id") or "") != artifact_id]
    rows.append(record)
    attrs["artifact_registry_refs"] = rows
    task.attributes = attrs


def _registry_workspace_root(task: SubAgentTask, path: Path | None) -> Path | None:
    if path is not None:
        for candidate in [*declared_workspace_roots(task), *artifact_candidate_roots(task)]:
            if _is_relative_to(path, candidate):
                return candidate
    for candidate in artifact_suffix_roots(task):
        if candidate.exists() and candidate.is_dir():
            return candidate
    return path.parent if path is not None else None


def normalize_artifact_ref(task: SubAgentTask, value: object) -> str:
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    try:
        path = Path(text).expanduser()
    except OSError:
        return text
    if path.is_absolute():
        if path.exists():
            return str(path)
        child_ref = _resolve_child_artifact_ref(task, text)
        return str(child_ref) if child_ref is not None else text
    resolved = _resolve_relative_artifact(task, path)
    if resolved is not None:
        return str(resolved)
    child_ref = _resolve_child_artifact_ref(task, text)
    return str(child_ref) if child_ref is not None else text


def _resolve_relative_artifact(task: SubAgentTask, path: Path) -> Path | None:
    direct_roots = artifact_candidate_roots(task)
    for root in direct_roots:
        candidate = root / path
        if candidate.exists():
            return candidate
    return _resolve_by_suffix(path, artifact_suffix_roots(task))


def _resolve_by_suffix(path: Path, roots: list[Path]) -> Path | None:
    parts = path.parts
    if not parts:
        return None
    matches: list[Path] = []
    for root in roots:
        matches.extend(item for item in root.rglob(parts[-1]) if _path_has_suffix(item, parts))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _resolve_child_artifact_ref(task: SubAgentTask, text: str) -> Path | None:
    name = _safe_path_name(text)
    if not name:
        return None
    matches: list[Path] = []
    for child_id in _child_ids_for_ref(task, text):
        matches.extend(_matching_child_artifact_refs(task, child_id, name))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _matching_child_artifact_refs(task: SubAgentTask, child_id: str, name: str) -> list[Path]:
    matches: list[Path] = []
    for ref in _child_task_artifact_refs(task, child_id):
        path = _existing_local_path(ref)
        if path is not None and path.name == name:
            matches.append(path)
    return matches


def _child_ids_for_ref(task: SubAgentTask, text: str) -> list[str]:
    child_ids = [str(item or "").strip() for item in getattr(task, "child_ids", []) or []]
    child_ids = [item for item in child_ids if item]
    hinted = [item for item in child_ids if item in text]
    return hinted or child_ids


def _child_task_artifact_refs(task: SubAgentTask, child_id: str) -> list[str]:
    child_task = _child_task_json_path(task, child_id)
    if child_task is None:
        return []
    try:
        payload = read_agent_state_payload(child_task)
    except (OSError, json.JSONDecodeError, TypeError, FileNotFoundError) as exc:
        _append_artifact_ref_load_error(task, child_id, child_task, exc)
        return []
    refs = payload.get("artifact_refs")
    if not isinstance(refs, list):
        return []
    return [str(ref or "").strip() for ref in refs if str(ref or "").strip()]


def _append_artifact_ref_load_error(
    task: SubAgentTask,
    child_id: str,
    path: Path,
    exc: BaseException,
) -> None:
    report = runtime_error_report(exc, context="subagent.artifact_refs.child_state")
    report["child_run_id"] = child_id
    report["path"] = str(path)
    raw_attrs = getattr(task, "attributes", {})
    attrs = dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
    existing = attrs.get("artifact_ref_load_errors")
    rows = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    dedupe_key = (report.get("context"), report.get("child_run_id"), report.get("path"))
    rows = [
        item
        for item in rows
        if (item.get("context"), item.get("child_run_id"), item.get("path")) != dedupe_key
    ]
    rows.append(report)
    attrs["artifact_ref_load_errors"] = rows
    task.attributes = attrs


def _child_task_json_path(task: SubAgentTask, child_id: str) -> Path | None:
    task_dir = _existing_local_path(getattr(task, "task_dir", ""))
    if task_dir is None:
        return None
    root = task_dir if task_dir.is_dir() else task_dir.parent
    candidate = root.parent / child_id / "task.json"
    return candidate if candidate.is_file() else None


def _existing_local_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    return path if path.exists() else None


def _safe_path_name(text: str) -> str:
    try:
        return Path(str(text or "").strip()).name
    except OSError:
        return ""


def _path_has_suffix(path: Path, parts: tuple[str, ...]) -> bool:
    return len(path.parts) >= len(parts) and path.parts[-len(parts):] == parts


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _artifact_claim(item: dict[str, object], ref: str) -> str:
    summary = str(item.get("summary") or "").strip()
    if summary:
        return f"artifact produced: {summary}"
    kind = str(item.get("kind") or "artifact").strip() or "artifact"
    return f"{kind} artifact produced: {ref}"


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


def synthesize_artifact_evidence_packets(
    task: SubAgentTask,
    artifacts: list[dict[str, object]],
    now: float,
) -> list[dict[str, object]]:
    packets: list[dict[str, object]] = []
    for item in artifacts:
        ref = normalize_artifact_ref(task, artifact_ref(item))
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


def merge_artifact_evidence(
    task: SubAgentTask,
    artifacts: list[dict[str, object]],
    evidence_packets: list[dict[str, object]],
    now: float,
) -> list[dict[str, object]]:
    artifacts = normalize_artifact_items(task, artifacts)
    if not evidence_packets:
        evidence_packets = synthesize_artifact_evidence_packets(task, artifacts, now)
    task.artifact_refs = _merge_list(
        task.artifact_refs,
        [ref for ref in (normalize_artifact_ref(task, artifact_ref(item)) for item in artifacts) if ref],
    )
    return evidence_packets
