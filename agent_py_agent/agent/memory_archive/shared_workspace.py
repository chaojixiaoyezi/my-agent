# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""task-local shared workspace facts for subagent collaboration.

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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 SharedWorkspaceResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SharedWorkspaceResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class SharedWorkspaceResult:
    """Concrete files for the task-local shared collaboration surface."""

    blackboard_md: Path
    messages_jsonl: Path
    findings_jsonl: Path
    evidence_packets_dir: Path
    evidence_index_jsonl: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 SyncSharedWorkspaceRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SyncSharedWorkspaceRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class SyncSharedWorkspaceRequest:
    """Bundle inputs for syncing a task-local shared workspace."""

    # LLM: shared workspace inputs stay task-local in one bundle and never imply main memory writes.
    task_workspace_root: Path
    task: Any
    now: float


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 sync_shared_workspace 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sync shared workspace 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_sync_request 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce sync request 涉及的字段，让后续匹配和存储使用同一形态。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 shared_workspace_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 shared workspace paths 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _message_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 message payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _finding_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 finding records 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_evidence_packets 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write evidence packets 相关记录，集中处理目标路径、格式化和状态更新。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _evidence_index_record 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 evidence index record 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_blackboard 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write blackboard 相关记录，集中处理目标路径、格式化和状态更新。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _record_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 record payload 相关记录，集中处理目标路径、格式化和状态更新。
def _record_payload(item: object) -> dict[str, object]:
    if is_dataclass(item):
        return asdict(item)
    return dict(item) if isinstance(item, dict) else {}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _claim_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 claim text 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _claim_text(item: object) -> str:
    payload = _record_payload(item)
    return str(payload.get("claim") or payload.get("id") or "未命名")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summary(task: Any) -> str:
    return str(getattr(task, "latest_summary", "") or getattr(task, "current_step", "") or getattr(task, "status", ""))[:500]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append_message 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append message 相关记录，集中处理目标路径、格式化和状态更新。
def _append_message(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_jsonl 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write jsonl 相关记录，集中处理目标路径、格式化和状态更新。
def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write json 相关记录，集中处理目标路径、格式化和状态更新。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write text 相关记录，集中处理目标路径、格式化和状态更新。
def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _safe_segment 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 safe segment 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _safe_segment(value: str) -> str:
    return str(value or "item").replace("/", "_").replace("\\", "_").strip() or "item"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _utc_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "SharedWorkspaceResult",
    "SyncSharedWorkspaceRequest",
    "shared_workspace_paths",
    "sync_shared_workspace",
]
