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
# 类用途: 保存 artifact 解析时允许访问的 task/run 根目录，防止 manifest 越界读取文件。
@dataclass(frozen=True)
class _ArtifactRecordContext:
    task_id: str
    run_id: str
    now: float
    allowed_roots: tuple[Path, ...]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _ResolvedArtifactRef 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 表示 artifact 引用的安全解析结果；调用方只在 status 为 resolved 时读取文件内容或计算 hash。
@dataclass(frozen=True)
class _ResolvedArtifactRef:
    path: Path | None
    status: str


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
    records = _artifact_records(
        inputs.task,
        inputs.now,
        task_workspace_root=inputs.task_workspace_root,
        agent_run_workspace_root=inputs.agent_run_workspace_root,
    )
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
# 函数用途: 把 artifact_refs 解析成受 workspace 边界保护的 manifest 记录，越界引用只登记不读取。
def _artifact_records(
    task: Any,
    now: float,
    *,
    task_workspace_root: Path,
    agent_run_workspace_root: Path,
) -> list[dict[str, object]]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    task_dir = Path(str(getattr(task, "task_dir", ""))) if getattr(task, "task_dir", "") else None
    context = _ArtifactRecordContext(
        task_id=task_id,
        run_id=run_id,
        now=now,
        # LLM: product roots granted to this run are safe for metadata only; bodies stay externalized.
        allowed_roots=_allowed_roots(
            task_dir,
            task_workspace_root,
            agent_run_workspace_root,
            getattr(task, "allowed_write_roots", []) or [],
        ),
    )
    records: list[dict[str, object]] = []
    for index, ref in enumerate(_artifact_refs(task), start=1):
        resolved = _resolve_ref(ref, context.allowed_roots)
        records.append(_artifact_record(context, index, ref, resolved))
    return records


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _artifact_record 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 artifact record 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _artifact_record(
    context: _ArtifactRecordContext,
    index: int,
    ref: str,
    resolved: _ResolvedArtifactRef,
) -> dict[str, object]:
    exists = bool(resolved.path and resolved.status == "resolved" and resolved.path.is_file())
    size_bytes = resolved.path.stat().st_size if exists and resolved.path else 0
    digest = _sha256_file(resolved.path) if exists and resolved.path else ""
    return {
        "version": 1,
        "artifact_id": _artifact_id(context.run_id, index, ref),
        "task_id": context.task_id,
        "run_id": context.run_id,
        "ref": ref,
        "path": str(resolved.path) if resolved.path else ref,
        "kind": _artifact_kind(ref),
        "resolution_status": resolved.status,
        "exists": exists,
        "size_bytes": size_bytes,
        "sha256": digest,
        "summary": _summary(ref, resolved.status, exists, size_bytes),
        "content_externalized": True,
        "source": "subagent_artifact_refs",
        "created_at": _utc_iso(context.now),
        "reserved": {},
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
# 函数用途: 在允许的 task/run 根目录内解析 artifact 引用，拒绝绝对路径和 .. 逃逸造成的越界读取。
def _resolve_ref(ref: str, allowed_roots: tuple[Path, ...]) -> _ResolvedArtifactRef:
    path = Path(ref).expanduser()
    if path.is_absolute():
        resolved = path.resolve(strict=False)
        return (
            _ResolvedArtifactRef(resolved, _path_status(resolved))
            if _is_under_allowed_root(resolved, allowed_roots)
            else _ResolvedArtifactRef(None, "blocked_outside_workspace")
        )
    for root in allowed_roots:
        candidate = (root / path).resolve(strict=False)
        if not _is_under_allowed_root(candidate, (root,)):
            continue
        if candidate.exists():
            return _ResolvedArtifactRef(candidate, _path_status(candidate))
    fallback = (allowed_roots[0] / path).resolve(strict=False) if allowed_roots else path
    if allowed_roots and not _is_under_allowed_root(fallback, allowed_roots):
        return _ResolvedArtifactRef(None, "blocked_outside_workspace")
    return _ResolvedArtifactRef(fallback, "missing")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _path_status 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 把已通过边界校验的 artifact 路径归类为可读文件、目录/特殊文件或缺失，供 manifest 明确展示。
def _path_status(path: Path) -> str:
    if path.is_file():
        return "resolved"
    if path.exists():
        return "not_file"
    return "missing"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _allowed_roots 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 归一化 legacy task、task workspace 和 agent run workspace 根目录，作为 artifact 解析的唯一允许边界。
def _allowed_roots(
    task_dir: Path | None,
    task_workspace_root: Path,
    agent_run_workspace_root: Path,
    allowed_write_roots: list[str],
) -> tuple[Path, ...]:
    # LLM: allowed_write_roots lets leaf product artifacts resolve without weakening outside-workspace blocking.
    roots = [
        item
        for item in [
            task_dir,
            task_workspace_root,
            agent_run_workspace_root,
            *[Path(str(root)) for root in allowed_write_roots if str(root or "").strip()],
        ]
        if item is not None
    ]
    normalized: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve(strict=False)
        if resolved not in normalized:
            normalized.append(resolved)
    return tuple(normalized)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _is_under_allowed_root 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断路径是否仍在允许根目录下，给绝对路径和相对路径解析共用同一安全规则。
def _is_under_allowed_root(path: Path, allowed_roots: tuple[Path, ...]) -> bool:
    for root in allowed_roots:
        try:
            path.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


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
# 函数用途: 根据安全解析状态生成短摘要，避免把越界 artifact 伪装成缺失文件。
def _summary(ref: str, status: str, exists: bool, size_bytes: int) -> str:
    if exists:
        return f"{Path(ref).name or ref} ({size_bytes} bytes)"
    if status == "blocked_outside_workspace":
        return f"blocked artifact ref outside workspace: {ref}"
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
