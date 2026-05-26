# LLM: Runner-declared artifacts must be checked as refs before closeout.
# 模块用途: 校验子代理结构化结果里声明的本地产物是否真实存在；只看路径，不读取产物正文。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .result_artifact_evidence import normalize_artifact_ref
from .result_artifact_roots import artifact_candidate_roots


# LLM: enforce_artifact_integrity marks missing local product refs as runner blockers.
# 函数用途: 子代理声称产出本地文件但文件不存在时，直接把本次 runner 结果转为 BLOCKED，防止父级提前读假产物。
def enforce_artifact_integrity(task: Any, parsed: Any, artifacts: list[dict[str, object]]) -> list[str]:
    if _already_blocked(parsed):
        return []
    missing = missing_local_artifact_refs(task, parsed, artifacts)
    if not missing:
        return []
    reason = "missing_artifact_refs: " + ", ".join(missing[:8])
    parsed.status = "BLOCKED"
    parsed.failure_type = "missing_artifact_refs"
    parsed.blocked_reason = reason
    blockers = getattr(task, "blockers", [])
    if reason not in blockers:
        blockers.append(reason)
        task.blockers = blockers
    return missing


# LLM: missing_local_artifact_refs collects local file refs from artifacts and evidence packets.
# 函数用途: 统一检查 artifacts.path 和 evidence_packets.artifact_refs，返回不存在的本地文件引用。
def missing_local_artifact_refs(task: Any, parsed: Any, artifacts: list[dict[str, object]]) -> list[str]:
    refs = _declared_artifact_refs(parsed, artifacts)
    missing: list[str] = []
    for ref in refs:
        normalized = normalize_artifact_ref(task, ref)
        if _local_ref_missing(task, normalized) and ref not in missing:
            missing.append(ref)
    return missing


# LLM: _declared_artifact_refs keeps explicit artifact paths from both structured sections.
# 函数用途: 收集模型声明的产物路径；artifact 协议、URL 和空值由后续本地路径判断处理。
def _declared_artifact_refs(parsed: Any, artifacts: list[dict[str, object]]) -> list[str]:
    refs: list[str] = []
    del artifacts
    for packet in getattr(parsed, "evidence_packets", []) or []:
        if not isinstance(packet, dict):
            continue
        for ref in packet.get("artifact_refs") or []:
            _append_ref(refs, ref)
    return refs


# LLM: _already_blocked keeps capability/request blockers from being overwritten by optional artifact notes.
# 函数用途: runner 已明确 BLOCKED/FAILED 或申请能力时，保留原始失败原因，不用 artifact 完整性二次覆盖。
def _already_blocked(parsed: Any) -> bool:
    status = str(getattr(parsed, "status", "") or "").strip().upper()
    if status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        return True
    if str(getattr(parsed, "blocked_reason", "") or "").strip():
        return True
    return bool(getattr(parsed, "capability_requests", []) or [])


# LLM: _local_ref_missing checks bounded task-local paths without scanning arbitrary roots.
# 函数用途: 判断一个 artifact ref 是否是缺失的本地文件；远程/protocol ref 不在这里判失败。
def _local_ref_missing(task: Any, ref: object) -> bool:
    text = str(ref or "").strip()
    if not text or "://" in text:
        return False
    path = Path(text).expanduser()
    if path.is_absolute():
        return not path.exists()
    for root in _candidate_roots(task):
        if (root / path).exists():
            return False
    return True


# LLM: _candidate_roots mirrors artifact resolution roots including runtime workspace/shared roots.
# 函数用途: 给相对产物路径提供 output/reports/run workspace/shared 等有限候选根，避免误扫用户整机。
def _candidate_roots(task: Any) -> list[Path]:
    return artifact_candidate_roots(task)


# LLM: _append_ref keeps first occurrence order stable for diagnostic messages.
# 函数用途: 追加非空唯一 ref，保持错误提示里的路径顺序可复现。
def _append_ref(refs: list[str], value: object) -> None:
    text = str(value or "").strip()
    if text and text not in refs:
        refs.append(text)
