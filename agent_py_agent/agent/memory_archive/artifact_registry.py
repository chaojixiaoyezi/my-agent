# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""normalized artifact manifests for runtime memory.

Human version:
Artifact manifests turn arbitrary `artifact_refs` into small records with
summary, hash, path, and existence metadata. The manifest stores references and
checksums only; large output bodies stay in artifact files.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ArtifactManifestResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ArtifactManifestResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ArtifactManifestResult:
    """Task/run manifest paths produced while syncing artifacts."""

    task_manifest_jsonl: Path
    agent_manifest_jsonl: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 SyncArtifactManifestsRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SyncArtifactManifestsRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class SyncArtifactManifestsRequest:
    """Bundle inputs for syncing task/run artifact manifests."""

    # LLM: artifact sync stays reference-only, so future fields belong on this explicit request.
    task: Any
    task_workspace_root: Path
    agent_run_workspace_root: Path
    now: float


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _ArtifactRecordContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _ArtifactRecordContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _ArtifactRecordContext:
    task_id: str
    run_id: str
    now: float


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 sync_artifact_manifests 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sync artifact manifests 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def sync_artifact_manifests(
    request: SyncArtifactManifestsRequest | Any = None,
    *,
    task: Any | None = None,
    task_workspace_root: Path | None = None,
    agent_run_workspace_root: Path | None = None,
    now: float | None = None,
) -> ArtifactManifestResult:
    """Write task-level and run-level artifact manifests from task artifact refs."""

    inputs = _coerce_sync_request(
        request,
        task=task,
        task_workspace_root=task_workspace_root,
        agent_run_workspace_root=agent_run_workspace_root,
        now=now,
    )
    records = _artifact_records(inputs.task, inputs.now)
    task_manifest = inputs.task_workspace_root / "artifacts" / "manifest.jsonl"
    agent_manifest = inputs.agent_run_workspace_root / "artifacts" / "manifest.jsonl"
    _write_manifest(task_manifest, records)
    _write_manifest(agent_manifest, records)
    return ArtifactManifestResult(task_manifest_jsonl=task_manifest, agent_manifest_jsonl=agent_manifest)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_sync_request 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce sync request 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_sync_request(
    request: SyncArtifactManifestsRequest | Any,
    *,
    task: Any | None,
    task_workspace_root: Path | None,
    agent_run_workspace_root: Path | None,
    now: float | None,
) -> SyncArtifactManifestsRequest:
    if isinstance(request, SyncArtifactManifestsRequest):
        return request
    resolved_task = request if request is not None else task
    if (
        resolved_task is None
        or task_workspace_root is None
        or agent_run_workspace_root is None
        or now is None
    ):
        raise TypeError(
            "sync_artifact_manifests requires task, task_workspace_root, "
            "agent_run_workspace_root, and now"
        )
    return SyncArtifactManifestsRequest(
        task=resolved_task,
        task_workspace_root=Path(task_workspace_root),
        agent_run_workspace_root=Path(agent_run_workspace_root),
        now=now,
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _artifact_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 artifact records 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _artifact_records(task: Any, now: float) -> list[dict[str, object]]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    task_dir = Path(str(getattr(task, "task_dir", ""))) if getattr(task, "task_dir", "") else None
    context = _ArtifactRecordContext(task_id=task_id, run_id=run_id, now=now)
    records: list[dict[str, object]] = []
    for index, ref in enumerate(_artifact_refs(task), start=1):
        resolved = _resolve_ref(ref, task_dir)
        records.append(_artifact_record(context, index, ref, resolved))
    return records


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _artifact_record 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 artifact record 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _artifact_record(
    context: _ArtifactRecordContext,
    index: int,
    ref: str,
    resolved: Path | None,
) -> dict[str, object]:
    exists = bool(resolved and resolved.is_file())
    size_bytes = resolved.stat().st_size if exists and resolved else 0
    digest = _sha256_file(resolved) if exists and resolved else ""
    return {
        "version": 1,
        "artifact_id": _artifact_id(context.run_id, index, ref),
        "task_id": context.task_id,
        "run_id": context.run_id,
        "ref": ref,
        "path": str(resolved) if resolved else ref,
        "kind": _artifact_kind(ref),
        "exists": exists,
        "size_bytes": size_bytes,
        "sha256": digest,
        "summary": _summary(ref, exists, size_bytes),
        "content_externalized": True,
        "source": "subagent_artifact_refs",
        "created_at": _utc_iso(context.now),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _artifact_refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 artifact refs 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _artifact_refs(task: Any) -> list[str]:
    refs: list[str] = []
    for item in list(getattr(task, "artifact_refs", []) or []):
        text = str(item).strip()
        if text and text not in refs:
            refs.append(text)
    return refs


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _resolve_ref 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 resolve ref 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _resolve_ref(ref: str, task_dir: Path | None) -> Path | None:
    path = Path(ref).expanduser()
    if path.is_absolute():
        return path
    return (task_dir / path) if task_dir else path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_manifest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write manifest 相关记录，集中处理目标路径、格式化和状态更新。
def _write_manifest(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _sha256_file 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sha256 file 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _artifact_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 artifact id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _artifact_id(run_id: str, index: int, ref: str) -> str:
    digest = hashlib.sha1(ref.encode("utf-8")).hexdigest()[:12]
    return f"artifact-{_safe_segment(run_id)}-{index}-{digest}"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _artifact_kind 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 artifact kind 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _artifact_kind(ref: str) -> str:
    suffix = Path(ref).suffix.lower()
    if suffix in {".json", ".md", ".txt", ".log"}:
        return "report"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    return "artifact"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summary(ref: str, exists: bool, size_bytes: int) -> str:
    if exists:
        return f"{Path(ref).name or ref} ({size_bytes} bytes)"
    return f"unresolved artifact ref: {ref}"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _utc_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _safe_segment 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 safe segment 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _safe_segment(value: str) -> str:
    return str(value or "item").replace("/", "_").replace("\\", "_").strip() or "item"


__all__ = [
    "ArtifactManifestResult",
    "SyncArtifactManifestsRequest",
    "sync_artifact_manifests",
]
