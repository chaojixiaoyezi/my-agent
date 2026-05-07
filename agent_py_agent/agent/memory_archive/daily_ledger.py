# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""append-only daily event ledger for runtime memory.

Human version:
The daily ledger is an index of what happened today. It stores compact task/run
facts and file references, not full subagent context or tool output bodies.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from ._storage_dates import _date_key
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

DAILY_LEDGER_EVENT_SCHEMA = RuntimeMemorySchemaOptions("daily_ledger_event")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 DailyLedgerAppendResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DailyLedgerAppendResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class DailyLedgerAppendResult:
    """Path and id for one appended daily event."""

    events_jsonl: Path
    event_id: str


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 DailyLedgerWorkspaceRefs 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DailyLedgerWorkspaceRefs 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class DailyLedgerWorkspaceRefs:
    """Task/run workspace roots referenced by a compact daily event."""

    task_workspace_root: Path
    agent_run_workspace_root: Path
    task_artifact_manifest_jsonl: Path | None = None
    # LLM: manifest refs point to summaries/hashes, never artifact bodies.
    agent_artifact_manifest_jsonl: Path | None = None
    # LLM: daily ledger only points at the compact ledger; run workspace owns the chain.
    agent_compaction_ledger_jsonl: Path | None = None
    # LLM: gate refs expose review queues, not promoted memory bodies.
    agent_memory_gate_candidates_jsonl: Path | None = None
    agent_skill_spark_gate_json: Path | None = None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 AppendSubagentTaskEventRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 AppendSubagentTaskEventRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class AppendSubagentTaskEventRequest:
    """Bundle inputs for appending one compact daily subagent event."""

    # LLM: daily ledger events accept one request bundle so refs stay grouped and auditable.
    root: str | Path
    task: Any
    workspace_refs: DailyLedgerWorkspaceRefs
    now: float


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 daily_events_path_for 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 daily events path for 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def daily_events_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:
    """Return `daily/YYYY-MM-DD/events.jsonl` under the runtime memory root."""

    return Path(root) / "daily" / _date_key(created_at) / "events.jsonl"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 append_subagent_task_event 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append subagent task event 相关记录，集中处理目标路径、格式化和状态更新。
def append_subagent_task_event(
    request: AppendSubagentTaskEventRequest | str | Path | None = None,
    task: Any | None = None,
    *,
    root: str | Path | None = None,
    workspace_refs: DailyLedgerWorkspaceRefs | None = None,
    now: float | None = None,
) -> DailyLedgerAppendResult:
    """Append a compact subagent task/run event to the daily ledger."""

    inputs = _coerce_append_request(
        request,
        task,
        root=root,
        workspace_refs=workspace_refs,
        now=now,
    )
    path = daily_events_path_for(inputs.root, inputs.now)
    payload = _event_payload(inputs.task, inputs.workspace_refs, inputs.now)
    append_jsonl(path, payload, sort_keys=True)
    return DailyLedgerAppendResult(events_jsonl=path, event_id=str(payload["event_id"]))


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _coerce_append_request 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce append request 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_append_request(
    request: AppendSubagentTaskEventRequest | str | Path | None,
    task: Any | None,
    *,
    root: str | Path | None,
    workspace_refs: DailyLedgerWorkspaceRefs | None,
    now: float | None,
) -> AppendSubagentTaskEventRequest:
    if isinstance(request, AppendSubagentTaskEventRequest):
        return request
    resolved_root = root if root is not None else request
    if resolved_root is None or task is None or workspace_refs is None or now is None:
        raise TypeError("append_subagent_task_event requires root, task, workspace_refs, and now")
    return AppendSubagentTaskEventRequest(
        root=resolved_root,
        task=task,
        workspace_refs=workspace_refs,
        now=now,
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _event_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 event payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _event_payload(task: Any, workspace_refs: DailyLedgerWorkspaceRefs, now: float) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "version": DAILY_LEDGER_EVENT_SCHEMA.version,
        "schema": runtime_memory_schema_payload(DAILY_LEDGER_EVENT_SCHEMA),
        "event_id": _event_id(task_id, run_id, now),
        "event_type": "subagent_task_saved",
        "created_at": _utc_iso(now),
        "task_id": task_id,
        "run_id": run_id,
        "parent_run_id": str(getattr(task, "parent_id", "")),
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "duration_seconds": _duration_seconds(task, now),
        "summary": _summary(task),
        "refs": _refs(task, workspace_refs),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "search": _search_fields(task),
        "reserved": runtime_memory_reserved_fields(DAILY_LEDGER_EVENT_SCHEMA),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 refs 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _refs(task: Any, workspace_refs: DailyLedgerWorkspaceRefs) -> dict[str, str]:
    legacy_task_dir = str(getattr(task, "task_dir", ""))
    task_workspace_root = workspace_refs.task_workspace_root
    agent_run_workspace_root = workspace_refs.agent_run_workspace_root
    return {
        "task_workspace": str(task_workspace_root),
        "task_state": str(task_workspace_root / "state.json"),
        "task_timeline": str(task_workspace_root / "timeline.jsonl"),
        "agent_run_workspace": str(agent_run_workspace_root),
        "agent_run_state": str(agent_run_workspace_root / "state.json"),
        "agent_run_timeline": str(agent_run_workspace_root / "timeline.jsonl"),
        "task_artifact_manifest": _path_text(workspace_refs.task_artifact_manifest_jsonl),
        "agent_artifact_manifest": _path_text(workspace_refs.agent_artifact_manifest_jsonl),
        "agent_compaction_ledger": _path_text(workspace_refs.agent_compaction_ledger_jsonl),
        "agent_memory_gate_candidates": _path_text(workspace_refs.agent_memory_gate_candidates_jsonl),
        "agent_skill_spark_gate": _path_text(workspace_refs.agent_skill_spark_gate_json),
        "legacy_task_dir": legacy_task_dir,
        "legacy_task_json": str(Path(legacy_task_dir) / "task.json") if legacy_task_dir else "",
        "legacy_checkpoint": str(getattr(task, "checkpoint_json", "")),
        "legacy_status_report": str(getattr(task, "status_report_json", "")),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _search_fields 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 search fields 的候选结果，并按参数完成筛选、排序或数量限制。
def _search_fields(task: Any) -> dict[str, object]:
    return {
        "agent_name": str(getattr(task, "agent_name", "")),
        "role": str(getattr(task, "role", "")),
        "status": str(getattr(task, "status", "")),
        "current_step": str(getattr(task, "current_step", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _path_text 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 path text 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _path_text(path: Path | None) -> str:
    return str(path) if path else ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _summary(task: Any) -> str:
    latest = str(getattr(task, "latest_summary", "")).strip()
    if latest:
        return latest[:500]
    current = str(getattr(task, "current_step", "")).strip()
    return current[:500] if current else str(getattr(task, "status", ""))[:500]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _duration_seconds 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 duration seconds 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _duration_seconds(task: Any, now: float) -> float:
    created_at = float(getattr(task, "created_at", 0.0) or now)
    return max(0.0, round(now - created_at, 3))


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _event_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 event id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _event_id(task_id: str, run_id: str, now: float) -> str:
    return f"evt-{_safe_segment(task_id)}-{_safe_segment(run_id)}-{int(now * 1000)}"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _utc_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _safe_segment 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 safe segment 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _safe_segment(value: str) -> str:
    return str(value or "item").replace("/", "_").replace("\\", "_").strip() or "item"


__all__ = [
    "AppendSubagentTaskEventRequest",
    "DailyLedgerAppendResult",
    "DailyLedgerWorkspaceRefs",
    "append_subagent_task_event",
    "daily_events_path_for",
]
