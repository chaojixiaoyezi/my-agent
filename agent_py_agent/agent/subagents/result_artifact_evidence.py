
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
from ..model_visible_refs import has_placeholder_path_segment
from ..runtime_errors import runtime_error_report
from .models import EvidencePacket, SubAgentTask
from .result_artifact_roots import (
    artifact_candidate_roots,
    artifact_suffix_roots,
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


# LLM: R4 子项④的搬运步骤，在 materialize 之前执行：把子代理锚定落点（output_alignment
#   的 delivery_map.from）的真实产物复制到声明意图位置（.to）。目标必须通过
#   _materializable_declared_output_path 的工作区围栏；已存在非空目标不覆盖。
#   搬运结果结构化记入 task.attributes["output_delivery_results"]，交付对账消费。
# 函数用途: 子代理 runner 结果写回时，把真实产物送到主代理声明的最终位置。
def deliver_anchored_outputs_to_declared(
    task: SubAgentTask,
    artifacts: list[dict[str, object]],
) -> list[dict[str, object]]:
    from .services.output_alignment import anchored_output_refs

    delivery_map = anchored_output_refs(task).delivery_map
    if not delivery_map:
        return []
    existing_refs = {artifact_ref(item) for item in artifacts}
    delivered: list[dict[str, object]] = []
    results: list[dict[str, str]] = []
    for entry in delivery_map:
        source_text = str(entry.get("from") or "")
        declared_text = str(entry.get("to") or "")
        target = _materializable_declared_output_path(task, declared_text)
        if target is None:
            results.append({"from": source_text, "to": declared_text, "status": "target_outside_workspace"})
            continue
        source = _existing_local_path(source_text)
        if source is None or not source.is_file():
            results.append({"from": source_text, "to": str(target), "status": "source_missing"})
            continue
        if target.resolve(strict=False) == source.resolve(strict=False):
            continue
        if target.exists() and target.stat().st_size > 0:
            results.append({"from": source_text, "to": str(target), "status": "skipped_existing"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copyfile(source, target)
        results.append({"from": source_text, "to": str(target), "status": "delivered"})
        if str(target) not in existing_refs:
            delivered.append(
                {
                    "path": str(target),
                    "kind": target.suffix.lstrip(".") or "file",
                    "summary": "delivered subagent output to declared location",
                    "source_ref": source_text,
                }
            )
    _record_delivery_results(task, results)
    return delivered


# 函数用途: 把搬运结果按结构化账本追加到 task.attributes（去重保序）。
def _record_delivery_results(task: SubAgentTask, results: list[dict[str, str]]) -> None:
    if not results:
        return
    attrs = dict(getattr(task, "attributes", {}) or {})
    existing = attrs.get("output_delivery_results")
    records = list(existing) if isinstance(existing, list) else []
    seen = {(str(r.get("from")), str(r.get("to")), str(r.get("status"))) for r in records if isinstance(r, dict)}
    for item in results:
        key = (item["from"], item["to"], item["status"])
        if key not in seen:
            records.append(item)
            seen.add(key)
    attrs["output_delivery_results"] = records
    task.attributes = attrs


def _normalized_artifact_item(task: SubAgentTask, item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    copied = dict(item)
    if has_placeholder_path_segment(copied.get("path")):
        return None
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


def _materializable_declared_output_path(task: SubAgentTask, ref: str) -> Path | None:
    if not ref or "://" in ref or has_placeholder_path_segment(ref):
        return None
    try:
        path = Path(ref).expanduser()
    except OSError:
        return None
    if not path.is_absolute():
        path = _relative_declared_output_path(task, path)
    if path is None:
        return None
    resolved = path.resolve(strict=False)
    return resolved if _inside_allowed_declared_output_root(task, resolved) else None


def _relative_declared_output_path(task: SubAgentTask, path: Path) -> Path | None:
    task_workspace = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if task_workspace:
        return Path(task_workspace).expanduser() / path
    task_dir = str(getattr(task, "task_dir", "") or "").strip()
    return Path(task_dir).expanduser().parent / path if task_dir else None


def _inside_allowed_declared_output_root(task: SubAgentTask, path: Path) -> bool:
    roots = [
        _path_or_none(getattr(task, "task_workspace_dir", "")),
        _path_or_none(getattr(task, "agent_run_workspace_dir", "")),
        _path_or_none(getattr(task, "task_dir", "")),
    ]
    for root in roots:
        if root is not None and _same_or_inside(path, root if root.is_dir() else root.parent):
            return True
    return False


def _path_or_none(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except OSError:
        return None


def _same_or_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _registry_workspace_root(task: SubAgentTask, path: Path | None) -> Path | None:
    task_workspace = _existing_local_path(getattr(task, "task_workspace_dir", ""))
    if task_workspace is not None and task_workspace.is_dir():
        return task_workspace
    task_dir = _existing_local_path(getattr(task, "task_dir", ""))
    if task_dir is not None:
        return task_dir if task_dir.is_dir() else task_dir.parent
    return path.parent if path is not None else None


def normalize_artifact_ref(task: SubAgentTask, value: object) -> str:
    text = str(value or "").strip()
    if not text or "://" in text or has_placeholder_path_segment(text):
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
