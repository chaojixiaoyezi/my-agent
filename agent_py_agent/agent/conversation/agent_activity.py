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

import threading
import time
from dataclasses import dataclass
from typing import Any

from ..subagents.services.control_plane_projection import (
    runtime_compact_count,
    runtime_token_count,
)
from .models import THREAD_TASK_LINK_ACTIVE_STATUS

_SCHEMA_VERSION = "conversation_agent_activity.v2"
_MAX_PROJECTED_SUBAGENTS = 64
_ACTIVITY_TEXT_LIMIT = 240
_MAIN_ACTIVITY_STATE_ATTR = "_conversation_main_activity_projection"
_MAIN_ACTIVITY_LOCK_ATTR = "_conversation_main_activity_projection_lock"
_MAIN_ACTIVITY_SETUP_LOCK = threading.Lock()


# LLM: This sink publishes volatile, display-only facts for one background owner
# turn. It cannot deliver messages, mutate task links, or decide completion.
# 类用途: 接收后台主代理真实的思考、工具和回复阶段，让 TUI/Web 能看到 main 正在做什么。
class BackgroundMainActivitySink:
    # LLM: Construction registers one exact thread/task identity; later updates
    # may replace only that thread's display row under the agent-owned lock.
    # 函数用途: 建立后台主代理活动接收器，并立即显示“整理任务上下文”。
    def __init__(self, agent: object, *, thread_id: str, task_id: str) -> None:
        self.agent = agent
        self.thread_id = str(thread_id or "").strip()
        self.task_id = str(task_id or "").strip()
        self.started_at = time.time()
        self._publish("running", "整理任务上下文")

    # LLM: Plain callback compatibility records only a generic phase; arbitrary
    # legacy text is never parsed into status or echoed into the public panel.
    # 函数用途: 兼容只会调用普通 callback 的模型/工具路径。
    def __call__(self, _text: str) -> None:
        self._publish("running", "处理中")

    # LLM: Model deltas indicate generation liveness only; answer content stays
    # in the normal committed transcript and is not duplicated into activity state.
    # 函数用途: 模型开始生成普通回复时更新 main 行。
    def write_model(self, _text: str) -> None:
        self._publish("responding", "正在生成回复")

    # LLM: Thinking text is bounded and whitespace-normalized for display; it
    # remains non-authoritative and never enters prompts or completion logic.
    # 函数用途: 把后台主代理最近一段真实思考显示在 main 行，避免用户误判卡死。
    def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> None:
        del duration_seconds
        activity = _bounded_text(text, limit=_ACTIVITY_TEXT_LIMIT)
        self._publish("thinking", activity or "思考中")

    # LLM: Tool activity reads only typed progress fields. Output, command text,
    # paths and errors are intentionally excluded from this compact public row.
    # 函数用途: 后台主代理调用工具时在 main 行显示工具名和阶段。
    def write_progress(self, progress: dict[str, object], _legacy_text: str = "") -> None:
        tool = _bounded_text(progress.get("tool"), limit=80)
        phase = str(progress.get("phase") or "").strip().lower()
        if tool and phase in {"finished", "completed", "succeeded", "failed"}:
            activity = f"已完成 {tool}"
        elif tool:
            activity = f"正在使用 {tool}"
        else:
            activity = "正在处理工具结果"
        self._publish("tool", activity)

    # LLM: Retry display uses typed attempt/delay facts only and returns True so
    # provider code does not fall back to leaking an exception string as prose.
    # 函数用途: 显示后台主代理的模型连接重试进度。
    def write_provider_retry(
        self,
        *,
        scope: str,
        attempt: int,
        total: int,
        delay_seconds: float,
        error_type: str,
    ) -> bool:
        del scope, error_type
        self._publish(
            "retrying",
            f"模型重连 {max(1, int(attempt))}/{max(1, int(total))}，等待 {max(0.0, float(delay_seconds)):.1f}s",
        )
        return True

    # LLM: Finish marks only rendering phase while the durable task link remains
    # active; the lifecycle owner will remove the panel after real closeout.
    # 函数用途: 模型轮返回后提示主代理正在提交最终结果。
    def finish(self) -> None:
        self._publish("finalizing", "整理最终回复")

    # LLM: Fail is a liveness hint only. The real exception/retry/task state is
    # still owned by the background runtime and structured lifecycle stores.
    # 函数用途: 后台主代理轮异常退出时让 main 行显示真实失败阶段。
    def fail(self) -> None:
        self._publish("failed", "本轮处理失败，等待底层恢复")

    # LLM: Updates are bounded scalar projections under one agent-owned lock;
    # callers cannot inject nested payloads or overwrite another thread key.
    # 函数用途: 原子更新当前 thread 的 main 活动快照。
    def _publish(self, phase: str, activity: str) -> None:
        if not self.thread_id:
            return
        lock = _main_activity_lock(self.agent)
        with lock:
            rows = _main_activity_rows(self.agent)
            rows[self.thread_id] = {
                "task_id": self.task_id,
                "phase": str(phase or "running"),
                "activity": _bounded_text(activity, limit=_ACTIVITY_TEXT_LIMIT),
                "started_at": self.started_at,
                "updated_at": time.time(),
            }


# LLM: The lock is stored on the shared Gateway agent so all scheduler threads
# serialize updates without creating a second service or durable authority.
# 函数用途: 取得后台 main 活动投影共用的线程锁。
def _main_activity_lock(agent: object) -> threading.Lock:
    lock = getattr(agent, _MAIN_ACTIVITY_LOCK_ATTR, None)
    if lock is not None:
        return lock
    with _MAIN_ACTIVITY_SETUP_LOCK:
        lock = getattr(agent, _MAIN_ACTIVITY_LOCK_ATTR, None)
        if lock is None:
            lock = threading.Lock()
            setattr(agent, _MAIN_ACTIVITY_LOCK_ATTR, lock)
    return lock


# LLM: The mutable mapping is private volatile display state keyed by thread id;
# only bounded copies cross the public activity endpoint.
# 函数用途: 取得或初始化 Gateway 进程内的后台 main 活动表。
def _main_activity_rows(agent: object) -> dict[str, dict[str, object]]:
    rows = getattr(agent, _MAIN_ACTIVITY_STATE_ATTR, None)
    if not isinstance(rows, dict):
        rows = {}
        setattr(agent, _MAIN_ACTIVITY_STATE_ATTR, rows)
    return rows


# LLM: Readback requires the exact active task identity when one is present;
# stale rows from an older task cannot appear under a new active conversation task.
# 函数用途: 读取当前 thread 对应的 main 活动快照，供 TUI/Web 状态接口展示。
def background_main_activity(
    agent: object,
    thread_id: str,
    active_task_ids: set[str],
) -> dict[str, object]:
    lock = _main_activity_lock(agent)
    with lock:
        row = dict(_main_activity_rows(agent).get(str(thread_id or ""), {}))
    task_id = str(row.get("task_id") or "").strip()
    if not row or (task_id and active_task_ids and task_id not in active_task_ids):
        return {}
    return {
        "task_id": task_id,
        "phase": _bounded_text(row.get("phase"), limit=40),
        "activity": _bounded_text(row.get("activity"), limit=_ACTIVITY_TEXT_LIMIT),
        "started_at": max(0.0, _safe_float(row.get("started_at"))),
        "updated_at": max(0.0, _safe_float(row.get("updated_at"))),
    }


# LLM: ConversationAgentActivity is an immutable, display-only projection. Lifecycle
# decisions must continue to read ThreadTaskLink and SubAgentTask, never this snapshot.
# 类用途: 汇总一个会话当前活跃主任务及其直属子代理，供 TUI 和后续 Web 共用。
@dataclass(frozen=True)
class ConversationAgentActivity:
    active_task_count: int = 0
    main_activity: dict[str, object] | None = None
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
            "main_activity": dict(self.main_activity or {}),
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
        main_activity=background_main_activity(agent, thread_id, set(active_task_ids)),
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
    status = str(getattr(task, "status", "") or "").strip().upper()
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
        "status": status,
        "activity": _activity_text(task, status=status),
        "current_tool": _bounded_text(getattr(task, "current_tool", ""), limit=80),
        "attempts": max(0, _safe_int(getattr(task, "runner_attempts", 0))),
        "token_count": max(0, runtime_token_count(task)),
        "compact_count": max(0, runtime_compact_count(task)),
        "created_at": max(0.0, _safe_float(getattr(task, "created_at", 0.0))),
        "updated_at": max(0.0, _safe_float(getattr(task, "updated_at", 0.0))),
        "heartbeat_at": max(0.0, _safe_float(getattr(task, "heartbeat_at", 0.0))),
        "ended_at": max(0.0, _safe_float(getattr(task, "ended_at", 0.0))),
    }


# LLM: Activity chooses already-persisted display context in a fixed order. The
# text may explain work to a human but must never drive status or recovery.
# 函数用途: 选择子代理最近最有用的一句活动说明。
# LLM: Terminal status suppresses stale current_tool/current_step text because
# those fields describe the last action, not what an ended child is doing now.
# 函数用途: 选择子代理最近最有用的一句活动说明；已结束时不再显示“模型已生成回复”等旧动作。
def _activity_text(task: object, *, status: str = "") -> str:
    if status not in {"PLANNING", "PENDING", "RUNNING", "BLOCKED", "PAUSED"}:
        return ""
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


__all__ = [
    "BackgroundMainActivitySink",
    "ConversationAgentActivity",
    "background_main_activity",
    "conversation_agent_activity",
]
