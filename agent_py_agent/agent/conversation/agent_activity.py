"""Canonical conversation-to-subagent activity projection.

This module only reads the conversation task links and subagent run records.  It
does not start, retry, stop, or otherwise mutate an agent.  TUI, Web, and IM
surfaces can therefore share one owner-scoped display snapshot without turning
the display cache into another lifecycle authority.
"""

# LLM: This module is the read-only adapter from canonical conversation task
# links and subagent runs to bounded public activity rows. It must never become
# a lifecycle, authorization, retry, or completion authority.
# 模块用途: 为 TUI 和后续 Web 提供同一份会话主任务与直属子代理状态快照。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import THREAD_TASK_LINK_ACTIVE_STATUS

_SCHEMA_VERSION = "conversation_agent_activity.v1"
_MAX_PROJECTED_SUBAGENTS = 64
_ACTIVITY_TEXT_LIMIT = 240


# LLM: ConversationAgentActivity is an immutable, display-only projection. Lifecycle
# decisions must continue to read ThreadTaskLink and SubAgentTask, never this snapshot.
# 类用途: 汇总一个会话当前活跃主任务及其直属子代理，供 TUI 和后续 Web 共用。
@dataclass(frozen=True)
class ConversationAgentActivity:
    active_task_count: int = 0
    subagents: tuple[dict[str, object], ...] = ()
    hidden_subagent_count: int = 0
    active_task_projection_ok: bool = True
    subagent_projection_ok: bool = True
    warnings: tuple[str, ...] = ()

    # LLM: JSON output contains only bounded public fields; canonical task/run
    # records and load errors remain in their existing stores.
    # 函数用途: 生成可直接由 Gateway 返回的有界 JSON 投影。
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "active_task_count": max(0, int(self.active_task_count or 0)),
            "subagents": [dict(row) for row in self.subagents],
            "hidden_subagent_count": max(0, int(self.hidden_subagent_count or 0)),
            "active_task_projection_ok": bool(self.active_task_projection_ok),
            "subagent_projection_ok": bool(self.subagent_projection_ok),
            "subagent_projection_warnings": list(self.warnings),
        }


# LLM: This is the single read adapter from conversation task identity to direct
# child run activity. It must not infer roots from prompt text or expose descendants
# outside the active, already-authenticated thread.
# 函数用途: 读取一个会话正在执行的任务，并列出这些任务直属子代理的实时状态。
def conversation_agent_activity(
    agent: object,
    store: object,
    thread_id: str,
) -> ConversationAgentActivity:
    active_task_ids, link_warnings = _active_task_ids(store, thread_id)
    if not active_task_ids:
        return ConversationAgentActivity(
            active_task_count=0,
            active_task_projection_ok=not link_warnings,
            warnings=tuple(link_warnings),
        )

    rows, run_warnings = _direct_subagent_rows(agent, set(active_task_ids))
    visible = rows[:_MAX_PROJECTED_SUBAGENTS]
    return ConversationAgentActivity(
        active_task_count=len(active_task_ids),
        subagents=tuple(visible),
        hidden_subagent_count=max(0, len(rows) - len(visible)),
        active_task_projection_ok=not link_warnings,
        subagent_projection_ok=not run_warnings,
        warnings=tuple(dict.fromkeys((*link_warnings, *run_warnings))),
    )


# LLM: active_task_ids is only a resumability index; count and roots must be
# filtered through each authoritative ThreadTaskLink status.
# 函数用途: 找出当前 thread 中状态仍为 active 的主任务 ID。
def _active_task_ids(store: object, thread_id: str) -> tuple[list[str], list[str]]:
    try:
        links, load_errors = store.active_task_links_report(thread_id)
    except Exception:
        return [], ["conversation_task_links_unavailable"]
    ids = [
        task_id
        for link in links
        if str(getattr(link, "status", "") or "").strip().lower()
        == THREAD_TASK_LINK_ACTIVE_STATUS
        if (task_id := str(getattr(link, "task_id", "") or "").strip())
    ]
    warnings = ["conversation_task_link_load_error"] if load_errors else []
    return list(dict.fromkeys(ids)), warnings


# LLM: Direct-child selection uses only root/parent/depth fields from canonical
# SubAgentTask rows. A grandchild remains visible through its own parent surface,
# not flattened into the main user's default list.
# 函数用途: 从子代理权威账中挑出当前主任务的直属子代理并生成有界展示行。
def _direct_subagent_rows(
    agent: object,
    root_task_ids: set[str],
) -> tuple[list[dict[str, object]], list[str]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return [], ["subagent_manager_unavailable"]
    try:
        report_method = getattr(manager, "list_runs_report", None)
        if callable(report_method):
            report = report_method()
            tasks = list(getattr(report, "runs", ()) or ())
            load_errors = list(getattr(report, "load_errors", ()) or ())
        else:
            tasks = list(manager.list_runs())
            load_errors = []
    except Exception:
        return [], ["subagent_runs_unavailable"]

    selected: list[object] = []
    for task in tasks:
        root_id = str(getattr(task, "root_id", "") or "").strip()
        parent_id = str(getattr(task, "parent_id", "") or "").strip()
        depth = _safe_int(getattr(task, "depth", 0))
        if root_id not in root_task_ids:
            continue
        if parent_id not in root_task_ids or depth != 1:
            continue
        selected.append(task)
    selected.sort(key=_subagent_sort_key)
    rows = [_subagent_row(task) for task in selected]
    warnings = ["subagent_run_load_error"] if load_errors else []
    return rows, warnings


# LLM: Sorting is presentation-only and reads canonical statuses/timestamps; it
# cannot change dispatch order or lifecycle priority.
# 函数用途: 让运行中和待处理的子代理排在已结束子代理前面。
def _subagent_sort_key(task: object) -> tuple[int, float, str]:
    status = str(getattr(task, "status", "") or "").strip().upper()
    priority = {
        "RUNNING": 0,
        "PENDING": 1,
        "PLANNING": 1,
        "BLOCKED": 2,
        "PAUSED": 2,
    }.get(status, 3)
    created_at = _safe_float(getattr(task, "created_at", 0.0))
    run_id = str(getattr(task, "id", "") or "")
    return priority, created_at, run_id


# LLM: This row deliberately omits goal, response, tool output, paths, and
# permissions. Human-readable activity is display context only, never a status source.
# 函数用途: 把一个直属子代理压成底栏需要的名字、状态、活动、耗时和尝试次数。
def _subagent_row(task: object) -> dict[str, object]:
    return {
        "run_id": str(getattr(task, "id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or ""),
        "parent_run_id": str(getattr(task, "parent_id", "") or ""),
        "depth": max(0, _safe_int(getattr(task, "depth", 0))),
        "name": _bounded_text(
            getattr(task, "agent_name", "") or getattr(task, "role", "") or "subagent",
            limit=80,
        ),
        "role": _bounded_text(getattr(task, "role", ""), limit=80),
        "status": str(getattr(task, "status", "") or "").strip().upper(),
        "activity": _activity_text(task),
        "current_tool": _bounded_text(getattr(task, "current_tool", ""), limit=80),
        "attempts": max(0, _safe_int(getattr(task, "runner_attempts", 0))),
        "created_at": max(0.0, _safe_float(getattr(task, "created_at", 0.0))),
        "updated_at": max(0.0, _safe_float(getattr(task, "updated_at", 0.0))),
        "heartbeat_at": max(0.0, _safe_float(getattr(task, "heartbeat_at", 0.0))),
        "ended_at": max(0.0, _safe_float(getattr(task, "ended_at", 0.0))),
    }


# LLM: Activity chooses already-persisted display context in a fixed order. The
# text may explain work to a human but must never drive status or recovery.
# 函数用途: 选择子代理最近最有用的一句活动说明。
def _activity_text(task: object) -> str:
    tool = _bounded_text(getattr(task, "current_tool", ""), limit=80)
    if tool:
        return f"正在使用 {tool}"
    for field_name in (
        "current_step",
        "last_progress_summary",
        "latest_summary",
        "description",
    ):
        text = _bounded_text(getattr(task, field_name, ""), limit=_ACTIVITY_TEXT_LIMIT)
        if text:
            return text
    return ""


# LLM: Display text is whitespace-normalized and bounded before crossing the
# Gateway. Terminal-control stripping still happens at the final TUI render chokepoint.
# 函数用途: 将活动文字压成一行并限制长度，避免底栏被长内容撑爆。
def _bounded_text(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# LLM: Numeric coercion is conservative and display-only; invalid data becomes
# zero and is never written back to canonical state.
# 函数用途: 安全读取展示所需的整数。
def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# LLM: Numeric coercion is conservative and display-only; invalid data becomes
# zero and is never written back to canonical state.
# 函数用途: 安全读取展示所需的时间戳。
def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["ConversationAgentActivity", "conversation_agent_activity"]
