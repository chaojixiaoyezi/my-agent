# LLM: Board item helpers isolate visual/status shaping from board service orchestration.
# 模块用途: 构建子代理看板条目、风险标记和筛选任务列表，让 board service 保持轻薄。

from __future__ import annotations

"""helpers for subagent board item construction."""

from pathlib import Path
from typing import Any

from ..models import SubAgentBoardOptions, SubAgentTask
from ..reports import SubAgentBoardItem
from .task_target_tokens import task_actual_target_tokens


# LLM: build_risk_flags derives board warnings from task state without mutating the task.
# 函数用途: 根据任务状态、证据、能力申请和通道状态生成看板风险标签。
def build_risk_flags(
    task: SubAgentTask,
    open_request_count: int,
    open_gap_count: int,
) -> list[str]:
    flags: list[str] = []
    if task.status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        flags.append(task.status.lower())
    if task.status == "DONE" and not task.evidence:
        flags.append("done_without_evidence")
    if task.status == "DONE" and task.verification_status != "VERIFIED":
        flags.append("done_without_verification")
    if open_request_count:
        flags.append("open_capability_request")
    if open_gap_count:
        flags.append("open_capability_gap")
    if task.takeover_by:
        flags.append("taken_over")
    if task.channel_status == "BROKEN":
        flags.append("channel_broken")
    if task.channel_status == "DEGRADED":
        flags.append("channel_degraded")
    return flags


# LLM: scoped_due_check_tasks keeps root-scoped/excluded due-check filtering in one helper.
# 函数用途: 过滤到期检查任务列表；root_id 限定任务树，exclude_run_ids 排除当前正在执行的父级。
def scoped_due_check_tasks(
    tasks: list[SubAgentTask],
    root_id: str,
    include_run_ids: list[str] | None = None,
    exclude_run_ids: list[str] | None = None,
) -> list[SubAgentTask]:
    normalized = str(root_id or "").strip()
    included = {str(item) for item in (include_run_ids or []) if str(item or "").strip()}
    excluded = {str(item) for item in (exclude_run_ids or []) if str(item or "").strip()}
    filtered = [task for task in tasks if task.id not in excluded]
    if included:
        filtered = [task for task in filtered if task.id in included]
    if not normalized:
        return filtered
    return [task for task in filtered if (task.root_id or task.id) == normalized]


# LLM: to_board_item converts a SubAgentTask into the stable SubAgentBoardItem schema.
# 函数用途: 转换看板条目的数据表示，保持跨模块传递时的字段含义一致。
def to_board_item(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
    include_child_status_counts: bool = True,
) -> SubAgentBoardItem:
    counts = _board_open_counts(task)
    child_counts = _board_child_counts(
        manager,
        task,
        task_index=task_index,
        include_child_status_counts=include_child_status_counts,
    )
    return SubAgentBoardItem(**_board_item_payload(task, counts, child_counts))


# LLM: board_options normalizes the legacy recent_limit argument into the options bundle.
# 函数用途: 让 build_board 的旧入口和新的 SubAgentBoardOptions 参数包共用同一处理逻辑。
def board_options(
    options: SubAgentBoardOptions | None,
    *,
    recent_limit: int,
) -> SubAgentBoardOptions:
    if options is not None:
        if not isinstance(options, SubAgentBoardOptions):
            raise TypeError("build_board requires options: SubAgentBoardOptions")
        return options
    return SubAgentBoardOptions(recent_limit=recent_limit)


# LLM: _board_open_counts keeps capability counters reusable across board payloads.
# 函数用途: 统计 OPEN capability request/gap 数量，供 risk flags 和 board item 共用。
def _board_open_counts(task: SubAgentTask) -> tuple[int, int]:
    open_request_count = sum(1 for item in task.capability_requests if item.status == "OPEN")
    open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
    return open_request_count, open_gap_count


# LLM: _board_child_counts keeps optional child status expansion isolated from item construction.
# 函数用途: 根据 board 选项决定是否统计直接 child 状态；默认保持 refs-only 摘要。
def _board_child_counts(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None,
    include_child_status_counts: bool,
) -> dict[str, int]:
    if not include_child_status_counts:
        return {}
    return _child_status_counts(manager, task, task_index=task_index)


# LLM: _board_item_payload maps SubAgentTask fields into the stable board item schema.
# 函数用途: 集中维护看板字段映射，避免 to_board_item 继续膨胀。
def _board_item_payload(
    task: SubAgentTask,
    counts: tuple[int, int],
    child_status_counts: dict[str, int],
) -> dict[str, object]:
    open_request_count, open_gap_count = counts
    return {
        "id": task.id,
        "root_id": task.root_id,
        "parent_id": task.parent_id,
        "depth": task.depth,
        "agent_name": task.agent_name,
        "role": task.role,
        "status": task.status,
        "verification_status": task.verification_status,
        "channel_status": task.channel_status,
        "owner": task.owner,
        "supervisor": task.supervisor,
        "final_owner": task.final_owner,
        "goal": task.goal,
        "updated_at": task.updated_at,
        "heartbeat_at": task.heartbeat_at,
        "evidence_count": len(task.evidence),
        "evidence_packet_count": len(task.evidence_packets),
        "finding_count": len(task.findings),
        "open_request_count": open_request_count,
        "open_gap_count": open_gap_count,
        "child_count": len(task.child_ids),
        "child_status_counts": child_status_counts,
        "progress": max(0.0, min(1.0, float(task.progress or 0.0))),
        "current_step": task.current_step,
        "latest_summary": task.latest_summary,
        "blocker_count": len(task.blockers),
        "takeover_by": task.takeover_by,
        "locked_file_count": len(task.locked_files),
        "risk_flags": build_risk_flags(task, open_request_count, open_gap_count),
        "task_dir": task.task_workspace_dir or task.task_dir,
        "output_json": task.agent_run_final_report_md or task.output_json,
        "task_workspace": task.task_workspace_dir,
        "agent_run_workspace": task.agent_run_workspace_dir,
        "legacy_task_dir": task.task_dir if task.task_dir != (task.task_workspace_dir or task.task_dir) else "",
        "legacy_output_json": task.output_json,
        "checkpoint_ref": task.agent_run_checkpoint_json or task.checkpoint_json or task.checkpoint_ref,
        "summary_ref": task.agent_run_summary_md,
        "final_report_ref": task.agent_run_final_report_md,
        "latest_tool_progress_ref": _latest_tool_progress_ref(task),
        "target_tokens": sorted(task_actual_target_tokens(task))[:20],
        "artifact_refs": _bounded_unique_strings(task.artifact_refs, limit=12),
        "artifact_registry_refs": _registry_records(task.attributes.get("artifact_registry_refs"), limit=12),
        "evidence_refs": _bounded_unique_strings(task.evidence_refs, limit=12),
    }


# LLM: _child_status_counts loads direct children only when the caller asked for expanded counts.
# 函数用途: 统计直接子级状态，缺失 child refs 统一计入 missing。
def _child_status_counts(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child_id in task.child_ids:
        try:
            child = task_index[child_id] if task_index is not None else manager.load(child_id)
        except (FileNotFoundError, TypeError, KeyError):
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        counts[child.status] = counts.get(child.status, 0) + 1
    return counts


# LLM: _bounded_unique_strings exposes refs to parents while keeping board rows small.
# 函数用途: 去重并限制 artifact/evidence refs 数量，避免看板因为大量产物引用撑爆上下文。
def _bounded_unique_strings(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _registry_records(value: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        artifact_id = str(row.get("artifact_id") or "").strip()
        path = str(row.get("path") or "").strip()
        key = artifact_id or path
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


# LLM: _latest_tool_progress_ref points parents at the current run progress snapshot, not legacy reports paths.
# 函数用途: 根据 agent_run_workspace_dir 派生 progress/latest_tool_progress.json，供看板返回可读进展入口。
def _latest_tool_progress_ref(task: SubAgentTask) -> str:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    return str(Path(workspace) / "progress" / "latest_tool_progress.json") if workspace else ""
