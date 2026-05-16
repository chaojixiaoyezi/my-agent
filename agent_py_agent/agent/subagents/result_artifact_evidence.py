# LLM: Artifact-only runner output needs a refs-only evidence bridge for parent acceptance.
# 模块用途: 从 runner artifacts 生成和合并证据引用；不读取 artifact 正文，只维护可追踪 refs。

from __future__ import annotations

from pathlib import Path

from .models import EvidencePacket, SubAgentTask
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
        return str(path) if path.exists() else text
    resolved = _resolve_relative_artifact(task, path)
    return str(resolved) if resolved is not None else text


# LLM: _resolve_relative_artifact searches only task-local roots so artifact repair stays bounded.
# 函数用途: 先按候选根拼接相对路径，再按文件名和后缀做有限恢复；不扫描用户整台机器。
def _resolve_relative_artifact(task: SubAgentTask, path: Path) -> Path | None:
    roots = _artifact_roots(task)
    for root in roots:
        candidate = root / path
        if candidate.exists():
            return candidate
    return _resolve_by_suffix(path, roots)


# LLM: _artifact_roots orders concrete run/workspace/shared roots before broad fallback roots.
# 函数用途: 返回当前 run 能合理产生产物的目录，包含 runtime workspace/shared/artifacts；allowed_write_roots 文件路径用父目录参与解析。
def _artifact_roots(task: SubAgentTask) -> list[Path]:
    raw_roots = [
        getattr(task, "output_dir", ""),
        getattr(task, "reports_dir", ""),
        getattr(task, "agent_run_workspace_dir", ""),
        getattr(task, "task_workspace_artifacts_dir", ""),
        getattr(task, "agent_run_artifacts_dir", ""),
        getattr(task, "task_workspace_shared_dir", ""),
        getattr(task, "task_workspace_dir", ""),
        getattr(task, "task_dir", ""),
        getattr(task, "data_dir", ""),
        getattr(task, "scratch_dir", ""),
        *(getattr(task, "allowed_write_roots", []) or []),
    ]
    roots: list[Path] = []
    for value in raw_roots:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
        except OSError:
            continue
        root = path if path.is_dir() else path.parent
        if root.exists() and root.is_dir() and root not in roots:
            roots.append(root)
    return roots


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
