# LLM: Artifact-only runner output needs a refs-only evidence bridge for parent acceptance.
# 模块用途: 从 runner artifacts 生成和合并证据引用；不读取 artifact 正文，只维护可追踪 refs。

from __future__ import annotations

import json
from pathlib import Path

from .models import EvidencePacket, SubAgentTask
from .result_artifact_roots import artifact_candidate_roots, artifact_suffix_roots
from .utils import _merge_list, _new_id


# LLM: artifact_ref extracts artifact pointers without expanding large files.
# 函数用途: 从 artifact metadata 读取 path/uri/artifact_id；保持 refs-only，供证据合成和 artifact_refs 合并使用。
def artifact_ref(item: dict[str, object]) -> str:
    return str(item.get("path") or item.get("uri") or item.get("artifact_id") or "").strip()


# LLM: normalize_artifact_items makes model-written short paths durable before parent closeout reads refs.
# 函数用途: 将 runner 输出里的相对产物路径解析成真实存在的任务本地路径；URL/artifact 协议引用保持原样。
def normalize_artifact_items(task: SubAgentTask, artifacts: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for item in artifacts:
        copied = _normalized_artifact_item(task, item)
        if copied is not None:
            normalized.append(copied)
    return normalized


# LLM: _normalized_artifact_item resolves one model-written artifact record.
# 函数用途: 只改第一条可解析 ref 字段；非 dict artifact 直接忽略，保持解析层宽容。
def _normalized_artifact_item(task: SubAgentTask, item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    copied = dict(item)
    for key in ("path", "file_path", "ref", "href"):
        resolved = normalize_artifact_ref(task, copied.get(key))
        if resolved:
            copied[key] = resolved
            break
    return copied


# LLM: normalize_artifact_ref resolves local artifact refs without reading file bodies or trusting arbitrary paths.
# 函数用途: 根据 task_dir/output_dir/reports_dir/allowed_write_roots 找到真实产物路径，找不到时保留原引用。
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


# LLM: _resolve_relative_artifact searches only task-local roots so artifact repair stays bounded.
# 函数用途: 先按候选根拼接相对路径，再按文件名和后缀做有限恢复；不扫描用户整台机器。
def _resolve_relative_artifact(task: SubAgentTask, path: Path) -> Path | None:
    direct_roots = artifact_candidate_roots(task)
    for root in direct_roots:
        candidate = root / path
        if candidate.exists():
            return candidate
    return _resolve_by_suffix(path, artifact_suffix_roots(task))


# LLM: _resolve_by_suffix repairs common short refs like report.md without broad text matching.
# 函数用途: 在当前任务根内按文件名查找，并要求真实路径以后缀匹配，避免误把同名无关文件当产物。
def _resolve_by_suffix(path: Path, roots: list[Path]) -> Path | None:
    parts = path.parts
    if not parts:
        return None
    matches: list[Path] = []
    for root in roots:
        matches.extend(item for item in root.rglob(parts[-1]) if _path_has_suffix(item, parts))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


# LLM: _resolve_child_artifact_ref trusts child task artifact_refs over parent-guessed paths.
# 函数用途: 父级 coordinator 猜错 child 文件目录时，按 child_id 和文件名回到 child task.json 的真实产物 refs。
def _resolve_child_artifact_ref(task: SubAgentTask, text: str) -> Path | None:
    name = _safe_path_name(text)
    if not name:
        return None
    matches: list[Path] = []
    for child_id in _child_ids_for_ref(task, text):
        matches.extend(_matching_child_artifact_refs(task, child_id, name))
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


# LLM: _matching_child_artifact_refs keeps child-ref matching shallow for guardrails.
# 函数用途: 返回某个直接 child 中与目标文件名匹配且真实存在的 artifact refs。
def _matching_child_artifact_refs(task: SubAgentTask, child_id: str, name: str) -> list[Path]:
    matches: list[Path] = []
    for ref in _child_task_artifact_refs(task, child_id):
        path = _existing_local_path(ref)
        if path is not None and path.name == name:
            matches.append(path)
    return matches


# LLM: _child_ids_for_ref narrows child artifact recovery when the bad ref includes a run id.
# 函数用途: 优先只查路径里出现的 child_id；没有明确 child_id 时才查全部直接 child，避免同名报告误配。
def _child_ids_for_ref(task: SubAgentTask, text: str) -> list[str]:
    child_ids = [str(item or "").strip() for item in getattr(task, "child_ids", []) or []]
    child_ids = [item for item in child_ids if item]
    hinted = [item for item in child_ids if item in text]
    return hinted or child_ids


# LLM: _child_task_artifact_refs reads a direct child's small task.json only.
# 函数用途: 获取直接 child 已验收登记的 artifact_refs；不扫描正文，不读大产物。
def _child_task_artifact_refs(task: SubAgentTask, child_id: str) -> list[str]:
    child_task = _child_task_json_path(task, child_id)
    if child_task is None:
        return []
    try:
        payload = json.loads(child_task.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    refs = payload.get("artifact_refs")
    if not isinstance(refs, list):
        return []
    return [str(ref or "").strip() for ref in refs if str(ref or "").strip()]


# LLM: _child_task_json_path derives the bounded sibling task.json location from parent task_dir.
# 函数用途: 只在当前 subagents 根下查直接 child 的 task.json，避免按用户文本做 glob 扫描。
def _child_task_json_path(task: SubAgentTask, child_id: str) -> Path | None:
    task_dir = _existing_local_path(getattr(task, "task_dir", ""))
    if task_dir is None:
        return None
    root = task_dir if task_dir.is_dir() else task_dir.parent
    candidate = root.parent / child_id / "task.json"
    return candidate if candidate.is_file() else None


# LLM: _existing_local_path normalizes local paths while ignoring protocols and invalid values.
# 函数用途: 判断 ref 是否是存在的本地路径；协议引用和坏路径返回 None。
def _existing_local_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text or "://" in text:
        return None
    try:
        path = Path(text).expanduser()
    except OSError:
        return None
    return path if path.exists() else None


# LLM: _safe_path_name extracts a filename without trusting malformed huge path text.
# 函数用途: 从模型上报 ref 中取最后文件名；坏路径返回空，防止异常中断验收。
def _safe_path_name(text: str) -> str:
    try:
        return Path(str(text or "").strip()).name
    except OSError:
        return ""


# LLM: _path_has_suffix keeps suffix repair deterministic and independent of platform separators.
# 函数用途: 判断候选真实文件路径是否以模型上报的相对路径片段结尾。
def _path_has_suffix(path: Path, parts: tuple[str, ...]) -> bool:
    return len(path.parts) >= len(parts) and path.parts[-len(parts):] == parts


# LLM: _artifact_claim keeps synthesized evidence concise for parent/verifier checks.
# 函数用途: 给系统补齐的 artifact evidence packet 生成 claim；优先使用模型提供的 summary。
def _artifact_claim(item: dict[str, object], ref: str) -> str:
    summary = str(item.get("summary") or "").strip()
    if summary:
        return f"artifact produced: {summary}"
    kind = str(item.get("kind") or "artifact").strip() or "artifact"
    return f"{kind} artifact produced: {ref}"


# LLM: _packet_payload mirrors the structured output evidence packet JSON shape.
# 函数用途: 把 EvidencePacket 转成 output.json 里的稳定字典格式，保持字段完整。
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


# LLM: synthesize_artifact_evidence_packets protects artifact-only successful work from false rejection.
# 函数用途: runner 已声明 artifacts 但漏写 evidence_packets 时，生成低置信度 refs-only 证据包并挂到 task。
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


# LLM: merge_artifact_evidence keeps artifact refs and synthesized packets in one small service boundary.
# 函数用途: 合并 artifact_refs；如果没有显式 evidence_packets，则从 artifacts 补齐 refs-only 证据包。
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
