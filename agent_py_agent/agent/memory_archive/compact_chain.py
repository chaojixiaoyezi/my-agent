# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""checkpoint-first compact chain records for agent run workspaces.

Human version:
This module writes a conservative compact chain for a subagent run. It records
checkpoint snapshots and compact metadata without deleting timelines, artifacts,
or legacy work-order files.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 CompactChainResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CompactChainResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class CompactChainResult:
    """Paths for the latest run-local compact checkpoint chain."""

    ledger_jsonl: Path
    latest_summary_md: Path
    latest_metadata_json: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 SyncAgentRunCompactChainRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SyncAgentRunCompactChainRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class SyncAgentRunCompactChainRequest:
    """Bundle inputs for appending one agent-run compact checkpoint."""

    # LLM: compact chain writes remain additive; request bundling avoids hidden positional drift.
    task: Any
    agent_run_workspace_root: Path
    artifact_manifest_jsonl: Path
    now: float


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _CompactSnapshotContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _CompactSnapshotContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _CompactSnapshotContext:
    event_id: str
    sequence: int
    previous_event_id: str
    agent_run_workspace_root: Path
    artifact_manifest_jsonl: Path
    summary_md: Path
    metadata_json: Path
    now: float
    state_fingerprint: str


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 sync_agent_run_compact_chain 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sync agent run compact chain 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def sync_agent_run_compact_chain(
    request: SyncAgentRunCompactChainRequest | Any = None,
    *,
    task: Any | None = None,
    agent_run_workspace_root: Path | None = None,
    artifact_manifest_jsonl: Path | None = None,
    now: float | None = None,
) -> CompactChainResult:
    """Append a checkpoint snapshot event and update latest compact refs."""

    inputs = _coerce_sync_request(
        request,
        task=task,
        agent_run_workspace_root=agent_run_workspace_root,
        artifact_manifest_jsonl=artifact_manifest_jsonl,
        now=now,
    )
    paths = _compact_paths(inputs.agent_run_workspace_root)
    paths.ledger_jsonl.parent.mkdir(parents=True, exist_ok=True)
    state_fingerprint = _state_fingerprint(inputs.task, inputs.artifact_manifest_jsonl)
    if _latest_state_fingerprint(paths.latest_metadata_json) == state_fingerprint:
        return paths
    sequence = _next_sequence(paths.ledger_jsonl)
    previous_event_id = _last_event_id(paths.ledger_jsonl)
    event_id = _event_id(str(getattr(inputs.task, "id", "")), sequence)
    event_summary = paths.ledger_jsonl.parent / f"{event_id}.md"
    event_metadata = paths.ledger_jsonl.parent / f"{event_id}.json"
    context = _CompactSnapshotContext(
        event_id=event_id,
        sequence=sequence,
        previous_event_id=previous_event_id,
        agent_run_workspace_root=inputs.agent_run_workspace_root,
        artifact_manifest_jsonl=inputs.artifact_manifest_jsonl,
        summary_md=event_summary,
        metadata_json=event_metadata,
        now=inputs.now,
        state_fingerprint=state_fingerprint,
    )
    metadata = _metadata_payload(inputs.task, context)
    summary = _summary_markdown(inputs.task, metadata)
    _write_markdown(event_summary, summary)
    _write_json(event_metadata, metadata)
    _write_markdown(paths.latest_summary_md, summary)
    _write_json(paths.latest_metadata_json, metadata)
    _append_ledger(paths.ledger_jsonl, metadata)
    _merge_checkpoint(inputs.agent_run_workspace_root / "checkpoint.json", metadata)
    return paths


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_sync_request 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce sync request 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_sync_request(
    request: SyncAgentRunCompactChainRequest | Any,
    *,
    task: Any | None,
    agent_run_workspace_root: Path | None,
    artifact_manifest_jsonl: Path | None,
    now: float | None,
) -> SyncAgentRunCompactChainRequest:
    if isinstance(request, SyncAgentRunCompactChainRequest):
        return request
    resolved_task = request if request is not None else task
    if (
        resolved_task is None
        or agent_run_workspace_root is None
        or artifact_manifest_jsonl is None
        or now is None
    ):
        raise TypeError(
            "sync_agent_run_compact_chain requires task, agent_run_workspace_root, "
            "artifact_manifest_jsonl, and now"
        )
    return SyncAgentRunCompactChainRequest(
        task=resolved_task,
        agent_run_workspace_root=Path(agent_run_workspace_root),
        artifact_manifest_jsonl=Path(artifact_manifest_jsonl),
        now=now,
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 default_compact_chain_result 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 default compact chain result 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def default_compact_chain_result(agent_run_workspace_root: Path) -> CompactChainResult:
    """Return compact-chain paths without writing them."""

    return _compact_paths(agent_run_workspace_root)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _compact_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 compact paths 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _compact_paths(agent_run_workspace_root: Path) -> CompactChainResult:
    compactions_dir = agent_run_workspace_root / "compactions"
    return CompactChainResult(
        ledger_jsonl=compactions_dir / "compaction_ledger.jsonl",
        latest_summary_md=compactions_dir / "latest_summary.md",
        latest_metadata_json=compactions_dir / "latest_metadata.json",
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _metadata_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 metadata payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _metadata_payload(
    task: Any,
    context: _CompactSnapshotContext,
) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "version": 1,
        "event_id": context.event_id,
        "event_type": "checkpoint_snapshot",
        "compact_status": "checkpoint_only",
        "sequence": context.sequence,
        "previous_event_id": context.previous_event_id,
        "task_id": task_id,
        "run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "summary": str(getattr(task, "latest_summary", "")),
        "refs": _refs(task, context),
        "created_at": _utc_iso(context.now),
        "state_fingerprint": context.state_fingerprint,
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 refs 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _refs(
    task: Any,
    context: _CompactSnapshotContext,
) -> dict[str, str]:
    root = context.agent_run_workspace_root
    return {
        "agent_run_workspace": str(root),
        "checkpoint": str(root / "checkpoint.json"),
        "summary": str(context.summary_md),
        "metadata": str(context.metadata_json),
        "compaction_ledger": str(root / "compactions" / "compaction_ledger.jsonl"),
        "artifact_manifest": str(context.artifact_manifest_jsonl),
        "timeline": str(root / "timeline.jsonl"),
        "legacy_checkpoint": str(getattr(task, "checkpoint_json", "")),
        "legacy_task_dir": str(getattr(task, "task_dir", "")),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summary_markdown 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summary markdown 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summary_markdown(task: Any, metadata: dict[str, object]) -> str:
    refs = metadata.get("refs") if isinstance(metadata.get("refs"), dict) else {}
    blockers = "\n".join(f"- {item}" for item in list(getattr(task, "blockers", []) or [])) or "- 暂无"
    artifact_refs = "\n".join(f"- {item}" for item in list(getattr(task, "artifact_refs", []) or [])) or "- 暂无"
    return (
        "# Compact Checkpoint Summary\n\n"
        f"- event_id: {metadata['event_id']}\n"
        f"- compact_status: {metadata['compact_status']}\n"
        f"- task_id: {metadata['task_id']}\n"
        f"- run_id: {metadata['run_id']}\n"
        f"- status: {metadata['status']}\n"
        f"- progress: {metadata['progress']}\n"
        f"- checkpoint: {refs.get('checkpoint', '')}\n"
        f"- compaction_ledger: {refs.get('compaction_ledger', '')}\n\n"
        "## Latest Summary\n\n"
        f"{metadata.get('summary') or '暂无'}\n\n"
        "## Blockers\n\n"
        f"{blockers}\n\n"
        "## Artifact Refs\n\n"
        f"{artifact_refs}\n\n"
        "## Next Recovery Step\n\n"
        "Read checkpoint.json first, then verify against timeline.jsonl, findings.jsonl, and artifact manifests.\n"
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _merge_checkpoint 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge checkpoint 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_checkpoint(path: Path, metadata: dict[str, object]) -> None:
    checkpoint = _read_json_object(path)
    refs = metadata.get("refs") if isinstance(metadata.get("refs"), dict) else {}
    # LLM: compact refs are additive recovery pointers; they never replace the original checkpoint facts.
    checkpoint["compact_chain"] = {
        "status": metadata.get("compact_status", ""),
        "last_event_id": metadata.get("event_id", ""),
        "ledger_ref": refs.get("compaction_ledger", ""),
        "summary_ref": refs.get("summary", ""),
        "metadata_ref": refs.get("metadata", ""),
        "artifact_manifest_ref": refs.get("artifact_manifest", ""),
        "timeline_ref": refs.get("timeline", ""),
        "content_preserved": True,
    }
    _write_json(path, checkpoint)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _next_sequence 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 next sequence 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _next_sequence(path: Path) -> int:
    return len(_ledger_lines(path)) + 1


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _last_event_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 last event id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _last_event_id(path: Path) -> str:
    for line in reversed(_ledger_lines(path)):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_id = str(payload.get("event_id") or "")
        if event_id:
            return event_id
    return ""


# LLM: _latest_state_fingerprint lets saves skip no-op checkpoint compact entries.
# 函数用途: 读取最新 checkpoint compact 的状态指纹；旧 metadata 没有该字段时返回空值以保持兼容。
def _latest_state_fingerprint(path: Path) -> str:
    return str(_read_json_object(path).get("state_fingerprint") or "")


# LLM: _state_fingerprint captures material recovery facts while ignoring updated_at/save churn.
# 函数用途: 生成 checkpoint compact 去重指纹；只有任务状态、摘要、阻塞、产物引用或 artifact manifest 变化才追加事件。
def _state_fingerprint(task: Any, artifact_manifest_jsonl: Path) -> str:
    payload = {
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": [str(item) for item in list(getattr(task, "blockers", []) or [])],
        "artifact_refs": [str(item) for item in list(getattr(task, "artifact_refs", []) or [])],
        "manifest_sha256": _file_sha256(artifact_manifest_jsonl),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# LLM: _file_sha256 keeps artifact manifest changes visible without embedding manifest bodies in metadata.
# 函数用途: 对 artifact manifest 做短指纹；文件不存在或读取失败时返回空字符串。
def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _ledger_lines 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 ledger lines 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _ledger_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append_ledger 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append ledger 相关记录，集中处理目标路径、格式化和状态更新。
def _append_ledger(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _read_json_object 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 read json object 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write json 相关记录，集中处理目标路径、格式化和状态更新。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_markdown 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write markdown 相关记录，集中处理目标路径、格式化和状态更新。
def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _event_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 event id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _event_id(run_id: str, sequence: int) -> str:
    return f"compact-{_safe_segment(run_id)}-{sequence:04d}"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _safe_segment 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 safe segment 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _safe_segment(value: str) -> str:
    return str(value or "run").replace("/", "_").replace("\\", "_").strip() or "run"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _utc_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "CompactChainResult",
    "SyncAgentRunCompactChainRequest",
    "default_compact_chain_result",
    "sync_agent_run_compact_chain",
]
