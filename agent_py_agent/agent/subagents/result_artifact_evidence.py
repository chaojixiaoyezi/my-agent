
"""Normalize subagent artifact refs onto the current task-local registry.

This module is deliberately read/registration oriented: it resolves refs that a
subagent reported, registers existing local artifacts, and preserves unresolved
text as recovery hints. It must not turn a missing artifact into a task failure;
closeout and the parent model decide what to do with incomplete delivery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def materialize_missing_declared_output_artifacts(
    task: SubAgentTask,
    parsed: Any,
    artifacts: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Write structured successful runner results into missing declared output slots."""

    declared = _declared_output_refs(task)
    if not declared:
        return []
    materialized: list[dict[str, object]] = []
    for ref in declared:
        target = _materializable_declared_output_path(task, ref)
        # target 存在就跳过(已有真文件);不存在则即使被"声明"过也要兜底——子代理声明 artifact
        # path 指向 output ≠ 真写到了那(T5 实测:子代理声明在 output、实际写在 work,旧
        # existing_refs 检查让这种"声明但没落"漏过兜底、output 遂为空、主代理读扑空瞎找)。
        if target is None or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = _copyable_text_artifact_source(target, artifacts)
        if source is None:
            # 回退(修 T5 实测:子代理把 declared 同名文件写到自己 work 目录、没落共享 output,
            # 致 declared 槽只能放 placeholder、主代理读桩瞎找 16 轮):扫 work 捞回同名文件。
            source = _recover_same_name_artifact_from_work(task, target)
        if source is not None:
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            summary = "copied subagent text artifact into declared output"
            placeholder = False
        else:
            target.write_text(_render_declared_output_markdown(task, parsed, ref), encoding="utf-8")
            summary = "materialized structured subagent result for declared output"
            # P2-2(R5a 实锤:交付区里多数"分析文件"是结构化摘要占位符,名义上
            # 占了交付位):兜底渲染的摘要不是真产物,带结构化标记供对账/报告单列,
            # 不允许冒充真交付。复制真实文本产物的不算占位符。
            placeholder = True
        entry: dict[str, object] = {
            "path": str(target),
            "kind": "md",
            "summary": summary,
        }
        if placeholder:
            entry["placeholder"] = True
            _record_placeholder_artifact(task, str(target))
        materialized.append(entry)
    return materialized


# LLM: P2-2 占位符账本:登记链只保留固定键,故占位事实独立记在
#   attributes.placeholder_artifacts(去重,环形上限 50),供 closeout 的
#   unresolved/open 投影单列与用户报告明示。只观测,不改对账判定。
# 函数用途: 记下"这个交付位放的是兜底摘要,不是真产物"。
def _record_placeholder_artifact(task: SubAgentTask, path_text: str) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    entries = attrs.get("placeholder_artifacts")
    entries = entries if isinstance(entries, list) else []
    if path_text not in entries:
        entries.append(path_text)
    attrs["placeholder_artifacts"] = entries[-50:]
    task.attributes = attrs


def _copyable_text_artifact_source(target: Path, artifacts: list[dict[str, object]]) -> Path | None:
    candidates = [_existing_local_path(artifact_ref(item)) for item in artifacts]
    candidates = [path for path in candidates if path is not None and path != target]
    text_candidates = [path for path in candidates if _text_artifact_suffix(path.suffix)]
    if not text_candidates:
        return None
    target_suffix = target.suffix.lower()
    same_kind = [path for path in text_candidates if path.suffix.lower() == target_suffix]
    selected = same_kind or text_candidates
    return selected[0] if len(selected) == 1 else None


def _text_artifact_suffix(suffix: str) -> bool:
    return suffix.lower() in {".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv"}


def _recover_same_name_artifact_from_work(task: SubAgentTask, target: Path) -> Path | None:
    """子代理把 declared output 同名文件写到了自己 work 目录(没落共享 output)时回退捞回。
    只认 basename 完全同名(强信号)+唯一+文本后缀,避免误取模板/状态文件;找不到唯一同名则
    返回 None(保持 placeholder、不瞎猜)。仅在声明 artifacts 里找不到可复制源时兜底调用。"""
    root = _path_or_none(getattr(task, "task_workspace_dir", ""))
    if root is None or not root.is_dir():
        return None
    matches = [
        path
        for path in root.rglob(target.name)
        if path.is_file() and path != target and _text_artifact_suffix(path.suffix)
    ]
    unique = list({path.resolve() for path in matches})
    return matches[0] if len(unique) == 1 else None


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


def _declared_output_refs(task: SubAgentTask) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    refs: list[str] = []
    for field in ("output_files", "output_refs"):
        refs.extend(_declared_output_ref_values(attrs.get(field)))
    return list(dict.fromkeys(item.strip() for item in refs if item.strip() and not has_placeholder_path_segment(item)))


def _declared_output_ref_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item or "") for item in value]
    return []


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


def _render_declared_output_markdown(task: SubAgentTask, parsed: Any, original_ref: str) -> str:
    lines = [
        "# Subagent Result",
        "",
        f"- run_id: {getattr(task, 'id', '')}",
        f"- status: {getattr(parsed, 'status', '')}",
        f"- declared_output_ref: {original_ref}",
        "",
    ]
    summary = str(getattr(parsed, "summary", "") or "").strip()
    if summary:
        lines.extend(["## Summary", "", summary, ""])
    _append_structured_list(lines, "Findings", getattr(parsed, "findings", []) or [])
    _append_structured_list(lines, "Evidence Packets", getattr(parsed, "evidence_packets", []) or [])
    _append_structured_list(lines, "Artifacts", getattr(parsed, "artifacts", []) or [])
    next_actions = [str(item or "").strip() for item in getattr(parsed, "next_actions", []) or [] if str(item or "").strip()]
    if next_actions:
        lines.extend(["## Next Actions", ""])
        lines.extend(f"- {item}" for item in next_actions)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _append_structured_list(lines: list[str], title: str, items: list[object]) -> None:
    rows = [item for item in items if item]
    if not rows:
        return
    lines.extend([f"## {title}", ""])
    for item in rows:
        if isinstance(item, dict):
            body = json.dumps(item, ensure_ascii=False, sort_keys=True)
        else:
            body = str(item)
        lines.append(f"- {body}")
    lines.append("")


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
