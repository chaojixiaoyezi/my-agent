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
from pathlib import Path
from typing import Any

from ..agent_core.runtime.task_identity import task_path_progress_ledger_id
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from ..subagents.services.control_plane_projection import (
    runtime_compact_count,
    runtime_context_token_count,
)
from ..task_progress import read_task_progress_report
from .agent_transcript import read_agent_transcript_events
from .background_transcript import BackgroundTranscriptSink
from .channels import project_user_reply, redact_host_absolute_paths
from .models import THREAD_TASK_LINK_ACTIVE_STATUS

_SCHEMA_VERSION = "conversation_agent_activity.v5"
_MAX_PROJECTED_SUBAGENTS = 64
_MAX_PROJECTED_PROGRESS_ITEMS = 128
_ACTIVITY_TEXT_LIMIT = 240
_CONTEXT_USAGE_SCHEMA = "model_visible_context_usage.v1"
_CONTEXT_USAGE_TOKEN_FIELDS = (
    "context_window_tokens",
    "compact_trigger_tokens",
    "current_tokens",
    "prompt_tokens",
    "messages_tokens",
    "runtime_guidance_tokens",
    "tool_schema_tokens",
)
_MAIN_ACTIVITY_STATE_ATTR = "_conversation_main_activity_projection"
_MAIN_ACTIVITY_LOCK_ATTR = "_conversation_main_activity_projection_lock"
_MAIN_ACTIVITY_SETUP_LOCK = threading.Lock()


# LLM: This sink publishes one scalar activity row and delegates public rich
# events to the bounded background transcript ring. Neither path may deliver
# messages, mutate task links, or decide completion.
# 类用途: 接收后台主代理真实的思考、工具和回复阶段，同时更新固定 main 行和可滚动过程正文。
class BackgroundMainActivitySink:
    # LLM: Construction registers one exact thread/task identity; later updates
    # may replace only that thread's display row under the agent-owned lock.
    # 函数用途: 建立后台主代理活动接收器，并立即显示“整理任务上下文”。
    def __init__(self, agent: object, *, thread_id: str, task_id: str) -> None:
        self.agent = agent
        self.thread_id = str(thread_id or "").strip()
        self.task_id = str(task_id or "").strip()
        self.started_at = time.time()
        self._transcript = BackgroundTranscriptSink(
            agent,
            thread_id=self.thread_id,
            task_id=self.task_id,
        )
        self._publish("running", "整理任务上下文")

    # LLM: Plain callback compatibility records only a generic phase; arbitrary
    # legacy text is never parsed into status or echoed into the public panel.
    # 函数用途: 兼容只会调用普通 callback 的模型/工具路径。
    def __call__(self, _text: str) -> None:
        self._publish("running", "处理中")

    # LLM: Model deltas indicate generation liveness only; answer content stays
    # in the normal committed transcript and is not duplicated into activity state.
    # 函数用途: 模型开始生成普通回复时更新 main 行。
    def write_model(self, text: str) -> None:
        self._transcript.write_model(text)
        self._publish("responding", "正在生成回复")

    # LLM: Streaming thinking is forwarded only through the explicit typed
    # provider callback; the scalar activity row remains a content-free liveness hint.
    # 函数用途: 实时显示后台主代理的显式思考增量，同时更新 main 活动行。
    def write_thinking_delta(self, text: str) -> bool:
        published = self._transcript.write_thinking_delta(text)
        if published:
            self._publish("thinking", "思考中")
        return published

    # LLM: Thinking text is bounded and whitespace-normalized for display; it
    # remains non-authoritative and never enters prompts or completion logic.
    # 函数用途: 把后台主代理最近一段真实思考显示在 main 行，避免用户误判卡死。
    def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> None:
        self._transcript.write_thinking(text, duration_seconds=duration_seconds)
        activity = _bounded_text(text, limit=_ACTIVITY_TEXT_LIMIT)
        self._publish("thinking", activity or "思考中")

    # LLM: Context usage is the same frozen numeric provider-preflight snapshot
    # used by foreground turns. It is display-only and must preserve the current
    # activity phase instead of creating a second context authority.
    # 函数用途: 保存后台 main 最近一次模型调用前的实时上下文总量，供 TUI 状态条刷新。
    def write_context_usage(self, usage: dict[str, object]) -> bool:
        public = _public_context_usage(usage)
        if not self.thread_id or not public:
            return False
        lock = _main_activity_lock(self.agent)
        with lock:
            rows = _main_activity_rows(self.agent)
            current = dict(rows.get(self.thread_id, {}))
            current.update(
                {
                    "task_id": self.task_id,
                    "started_at": current.get("started_at") or self.started_at,
                    "updated_at": time.time(),
                    "context_usage": public,
                }
            )
            rows[self.thread_id] = current
        return True

    # LLM: Tool activity reads only typed progress fields. The compact row keeps
    # a short tool phase while the separate ring receives the existing bounded
    # public output/display projection for rich rendering.
    # 函数用途: 后台主代理调用工具时更新 main 行，并把公开结果送入正文工具卡片。
    def write_progress(self, progress: dict[str, object], _legacy_text: str = "") -> None:
        self._transcript.write_progress(progress)
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
        self._transcript.write_provider_retry(
            attempt=attempt,
            total=total,
            delay_seconds=delay_seconds,
        )
        self._publish(
            "retrying",
            f"模型重连 {max(1, int(attempt))}/{max(1, int(total))}，等待 {max(0.0, float(delay_seconds)):.1f}s",
        )
        return True

    # LLM: Native IR compaction is forwarded as public numeric transcript data;
    # the scalar main row remains a liveness hint and cannot advance generations.
    # 函数用途: 在后台正文显示一次真实工具上下文裁剪。
    def write_context_compaction(self, value: dict[str, object]) -> bool:
        published = self._transcript.write_context_compaction(value)
        if published:
            self._publish("compacting", "正在整理上下文")
        return published

    # LLM: Durable Compact progress shares the foreground TUI block protocol;
    # this callback does not write checkpoints or infer phase from display text.
    # 函数用途: 把后台会话 Compact 的结构化进度送进正文进度块。
    def write_conversation_compact_progress(self, value: dict[str, object]) -> bool:
        published = self._transcript.write_conversation_compact_progress(value)
        if published:
            self._publish("compacting", "正在压缩会话上下文")
        return published

    # LLM: Finish marks only rendering phase while the durable task link remains
    # active; one model turn ending does not prove the whole task is final.
    # 函数用途: 模型轮返回后显示等待后续事件，避免有活跃 child 时误报“整理最终回复”。
    def finish(self) -> None:
        self._transcript.finish()
        self._publish("waiting", "等待后续事件")

    # LLM: Fail is a liveness hint only. The real exception/retry/task state is
    # still owned by the background runtime and structured lifecycle stores.
    # 函数用途: 后台主代理轮异常退出时让 main 行显示真实失败阶段。
    def fail(self) -> None:
        self._transcript.fail()
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
            current = dict(rows.get(self.thread_id, {}))
            row = {
                "task_id": self.task_id,
                "phase": str(phase or "running"),
                "activity": _bounded_text(activity, limit=_ACTIVITY_TEXT_LIMIT),
                "started_at": self.started_at,
                "updated_at": time.time(),
            }
            if usage := _public_context_usage(current.get("context_usage")):
                row["context_usage"] = usage
            rows[self.thread_id] = row


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
    public = {
        "task_id": task_id,
        "phase": _bounded_text(row.get("phase"), limit=40),
        "activity": _bounded_text(row.get("activity"), limit=_ACTIVITY_TEXT_LIMIT),
        "started_at": max(0.0, _safe_float(row.get("started_at"))),
        "updated_at": max(0.0, _safe_float(row.get("updated_at"))),
    }
    if usage := _public_context_usage(row.get("context_usage")):
        public["context_usage"] = usage
    return public


# LLM: ConversationAgentActivity is an immutable, display-only projection. Lifecycle
# decisions must continue to read ThreadTaskLink and SubAgentTask, never this snapshot.
# 类用途: 汇总一个会话当前活跃主任务及其直属子代理，供 TUI 和后续 Web 共用。
@dataclass(frozen=True)
class ConversationAgentActivity:
    active_task_count: int = 0
    compact_count: int = 0
    main_activity: dict[str, object] | None = None
    subagents: tuple[dict[str, object], ...] = ()
    task_progress_items: tuple[dict[str, object], ...] = ()
    hidden_subagent_count: int = 0
    active_task_projection_ok: bool = True
    subagent_projection_ok: bool = True
    task_progress_projection_ok: bool = True
    warnings: tuple[str, ...] = ()

    # LLM: JSON output contains only bounded public fields; canonical task/run
    # records and load errors remain in their existing stores.
    # 函数用途: 生成可直接由 Gateway 返回的有界 JSON 投影。
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "active_task_count": max(0, int(self.active_task_count or 0)),
            "compact_count": max(0, int(self.compact_count or 0)),
            "main_activity": dict(self.main_activity or {}),
            "subagents": [dict(row) for row in self.subagents],
            "task_progress_items": [dict(row) for row in self.task_progress_items],
            "hidden_subagent_count": max(0, int(self.hidden_subagent_count or 0)),
            "active_task_projection_ok": bool(self.active_task_projection_ok),
            "subagent_projection_ok": bool(self.subagent_projection_ok),
            "task_progress_projection_ok": bool(self.task_progress_projection_ok),
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
    compact_count, compact_warnings = _conversation_compact_count(store, thread_id)
    active_links, link_warnings = _active_task_links(store, thread_id)
    active_task_ids = [
        str(getattr(link, "task_id", "") or "").strip() for link in active_links
    ]
    display_links = list(active_links)
    if not display_links:
        retained_link, retained_warnings = _retained_workspace_task_link(store, thread_id)
        link_warnings.extend(retained_warnings)
        if retained_link is not None:
            display_links.append(retained_link)
    display_task_ids = [
        str(getattr(link, "task_id", "") or "").strip() for link in display_links
    ]
    if not display_task_ids:
        return ConversationAgentActivity(
            active_task_count=0,
            compact_count=compact_count,
            active_task_projection_ok=not link_warnings,
            warnings=tuple(dict.fromkeys((*link_warnings, *compact_warnings))),
        )

    rows, run_warnings = _direct_subagent_rows(
        agent,
        set(display_task_ids),
        conversation_store=store,
    )
    progress_items, progress_warnings = _task_progress_items_from_links(
        agent,
        display_links,
        preferred_task_id=_conversation_workspace_task_id(store, thread_id),
        hidden_item_ids=_row_run_ids(rows),
    )
    visible = rows[:_MAX_PROJECTED_SUBAGENTS]
    return ConversationAgentActivity(
        active_task_count=len(active_task_ids),
        compact_count=compact_count,
        main_activity=(
            background_main_activity(agent, thread_id, set(active_task_ids))
            if active_task_ids
            else {}
        ),
        subagents=tuple(visible),
        task_progress_items=progress_items,
        hidden_subagent_count=max(0, len(rows) - len(visible)),
        active_task_projection_ok=not link_warnings,
        subagent_projection_ok=not run_warnings,
        task_progress_projection_ok=not progress_warnings,
        warnings=tuple(
            dict.fromkeys(
                (*link_warnings, *run_warnings, *progress_warnings, *compact_warnings)
            )
        ),
    )


# LLM: Child detail is a bounded owner-facing projection over one exact canonical
# run. It reads lifecycle, the independent ConversationThread, Todo ledger, and
# public event stream but cannot authorize, mutate, resume, or complete the run.
# 函数用途: 返回一个子代理详情页所需的任务说明、状态、直属下级、上下文、过程事件和最终回复。
def conversation_agent_view(
    agent: object,
    store: object,
    run_id: str,
    *,
    after: int = 0,
) -> dict[str, object]:
    selected = str(run_id or "").strip()
    manager = getattr(agent, "subagents", None)
    if manager is None or not selected:
        raise FileNotFoundError(selected or "subagent")
    task = manager.load(selected)
    row = _subagent_row(task, conversation_store=store)
    rows, warnings = _child_subagent_rows(
        agent,
        task,
        conversation_store=store,
    )
    progress_items, progress_warnings = _task_progress_items_for_run(
        agent,
        selected,
        hidden_item_ids=_row_run_ids(rows),
    )
    transcript = read_agent_transcript_events(
        agent,
        run_id=selected,
        after=after,
    )
    thread_id = str(getattr(task, "agent_thread_id", "") or "").strip()
    final_response, history_warnings = _agent_final_response(store, thread_id)
    status = str(getattr(task, "status", "") or "").strip().upper()
    terminal = task_status_in(status, SUBAGENT_ENDED_STATUSES)
    transcript_warnings = (
        ["agent_transcript_load_error"] if transcript.get("load_errors") else []
    )
    return {
        "schema_version": "conversation_agent_view.v1",
        "ok": True,
        "agent": {
            **row,
            "goal": _public_agent_text(
                getattr(task, "description", "") or getattr(task, "goal", ""),
                limit=4_000,
            ),
            "activity": _subagent_current_activity(task),
            "context_usage": _subagent_context_usage(task),
        },
        "terminal": terminal,
        "children": rows[:_MAX_PROJECTED_SUBAGENTS],
        "hidden_child_count": max(0, len(rows) - _MAX_PROJECTED_SUBAGENTS),
        "task_progress_items": list(progress_items),
        "transcript_events": list(transcript.get("events") or []),
        "event_cursor": max(0, _safe_int(transcript.get("cursor"))),
        "events_truncated": bool(transcript.get("truncated")),
        # 运行中的历史 assistant 段可能只是 Compact 前一轮，不能冒充当前任务 final。
        "final_response": final_response if terminal else "",
        "warnings": list(
            dict.fromkeys(
                (
                    *warnings,
                    *progress_warnings,
                    *history_warnings,
                    *transcript_warnings,
                )
            )
        ),
    }


# LLM: The durable ConversationThread generation is the only main-agent compact counter;
# display consumers must not count compact progress events or infer completions from text.
# 函数用途: 从当前会话权威 thread 读取已经成功提交的 Compact 次数，读取失败只产生展示告警。
def _conversation_compact_count(
    store: object,
    thread_id: str,
) -> tuple[int, list[str]]:
    loader = getattr(store, "load_thread_report", None)
    if not callable(loader):
        return 0, []
    try:
        thread, load_error = loader(thread_id)
    except Exception:
        return 0, ["conversation_thread_unavailable"]
    count = max(0, _safe_int(getattr(thread, "compact_generation", 0)))
    warnings = ["conversation_thread_load_error"] if load_error else []
    return count, warnings


# LLM: active_task_ids is only a resumability index; every returned row is
# filtered through its authoritative ThreadTaskLink status before projection.
# 函数用途: 找出当前 thread 中状态仍为 active 的主任务记录。
def _active_task_links(store: object, thread_id: str) -> tuple[list[object], list[str]]:
    try:
        links, load_errors = store.active_task_links_report(thread_id)
    except Exception:
        return [], ["conversation_task_links_unavailable"]
    active = [
        link
        for link in links
        if str(getattr(link, "status", "") or "").strip().lower()
        == THREAD_TASK_LINK_ACTIVE_STATUS
        if str(getattr(link, "task_id", "") or "").strip()
    ]
    warnings = ["conversation_task_link_load_error"] if load_errors else []
    deduplicated = {
        str(getattr(link, "task_id", "") or "").strip(): link for link in active
    }
    return list(deduplicated.values()), warnings


# LLM: Completed child rows remain discoverable through the thread's canonical
# workspace_task_id only when its task link belongs to this exact thread. This
# is a read projection and cannot reactivate an inactive link.
# 函数用途: 主任务结束后保留最近一次任务的子代理名册，供方向键进入查看历史。
def _retained_workspace_task_link(
    store: object,
    thread_id: str,
) -> tuple[object | None, list[str]]:
    loader = getattr(store, "load_thread_report", None)
    link_loader = getattr(store, "load_task_link_report", None)
    if not callable(loader) or not callable(link_loader):
        return None, []
    try:
        thread, thread_error = loader(thread_id)
    except Exception:
        return None, ["conversation_thread_unavailable"]
    task_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    if not task_id:
        return None, ["conversation_thread_load_error"] if thread_error else []
    try:
        link, link_error = link_loader(task_id)
    except Exception:
        return None, ["conversation_task_link_load_error"]
    if (
        link is None
        or str(getattr(link, "thread_id", "") or "").strip()
        != str(thread_id or "").strip()
    ):
        return None, ["conversation_workspace_task_mismatch"]
    warnings = []
    if thread_error or link_error:
        warnings.append("conversation_task_link_load_error")
    return link, warnings


# LLM: Final notice and live activity must read the same canonical progress
# ledger. This public helper accepts only an exact task link identity and never
# mutates progress or decides task completion.
# 函数用途: 读取某个会话任务当前的 Todo 快照，供后台最终消息补齐最后一次界面刷新。
def task_progress_items_for_task(
    agent: object,
    store: object,
    task_id: str,
) -> tuple[dict[str, object], ...]:
    loader = getattr(store, "load_task_link", None)
    if not callable(loader) or not str(task_id or "").strip():
        return ()
    try:
        link = loader(str(task_id).strip())
    except Exception:
        return ()
    rows, _run_warnings = _direct_subagent_rows(
        agent,
        {str(task_id).strip()},
        conversation_store=store,
    )
    items, _warnings = _task_progress_items_from_links(
        agent,
        [link] if link else [],
        hidden_item_ids=_row_run_ids(rows),
    )
    return items


# LLM: The ConversationThread workspace_task_id is the canonical Todo owner when
# it matches an active link. Child links share the thread but must never replace
# the root ledger merely because they were created later. Rows still come only
# from task_progress.v1 and are reduced to stable display fields.
# 函数用途: 按会话明确登记的主任务选择权威进度账本，再压成 TUI 可展示的清单。
def _task_progress_items_from_links(
    agent: object,
    links: list[object],
    *,
    preferred_task_id: str = "",
    hidden_item_ids: set[str] | None = None,
) -> tuple[tuple[dict[str, object], ...], list[str]]:
    candidates = [link for link in links if link is not None]
    if not candidates:
        return (), []
    preferred = str(preferred_task_id or "").strip()
    link = next(
        (
            item
            for item in candidates
            if preferred
            and str(getattr(item, "task_id", "") or "").strip() == preferred
        ),
        None,
    )
    if link is None:
        link = max(
            candidates,
            key=lambda item: _safe_float(getattr(item, "created_at", 0.0)),
        )
    task_path = str(getattr(link, "task_path", "") or "").strip()
    owner_root = _owner_runtime_root(agent)
    if not task_path or owner_root is None:
        return (), []
    ledger_id = task_path_progress_ledger_id(task_path)
    progress, load_error = read_task_progress_report(owner_root, ledger_id)
    if load_error:
        return (), ["task_progress_load_error"]
    raw_items = progress.get("items") if isinstance(progress, dict) else None
    if not isinstance(raw_items, list | tuple):
        return (), []
    hidden = hidden_item_ids or set()
    rows: list[dict[str, object]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_id = _bounded_text(item.get("id"), limit=128)
        if not item_id or item_id in hidden:
            continue
        rows.append(
            {
                "id": item_id,
                "title": _bounded_text(item.get("title"), limit=_ACTIVITY_TEXT_LIMIT),
                "status": _bounded_text(item.get("status"), limit=32) or "pending",
            }
        )
        if len(rows) >= _MAX_PROJECTED_PROGRESS_ITEMS:
            break
    return tuple(rows), []


# LLM: Todo ownership is an exact join to ConversationThread.workspace_task_id.
# Missing legacy/fake store support returns no preference and lets the bounded
# link reader keep its previous fallback; prompt text and task paths are never parsed.
# 函数用途: 从会话权威记录读取当前主任务 ID，防止较晚创建的子代理抢走 Todo 面板。
def _conversation_workspace_task_id(store: object, thread_id: str) -> str:
    loader = getattr(store, "load_thread_report", None)
    if not callable(loader):
        return ""
    try:
        thread, _load_error = loader(thread_id)
    except Exception:
        return ""
    return str(getattr(thread, "workspace_task_id", "") or "").strip()


# LLM: Child identity joins are exact run-id joins. This helper must never use
# descriptions, goals, titles, roles, or status prose to decide Todo visibility.
# 函数用途: 从直属子代理展示行提取精确 run_id，供 Todo 视图去掉重复的自动派工项。
def _row_run_ids(rows: list[dict[str, object]]) -> set[str]:
    return {
        str(row.get("run_id") or "").strip()
        for row in rows
        if str(row.get("run_id") or "").strip()
    }


# LLM: Owner root lookup follows the same structured home/root fallback as the
# progress tool but remains read-only and never creates directories.
# 函数用途: 找到 task_progress 账本所在的 owner 根目录。
def _owner_runtime_root(agent: object) -> Path | None:
    owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    root = owner_home or getattr(agent, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


# LLM: Numeric context sanitization mirrors the public Gateway contract. It
# rejects the wrong schema and strips all content-bearing or unknown values.
# 函数用途: 清洗后台 main 的实时上下文快照，只保留数字和协议标记。
def _public_context_usage(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema") != _CONTEXT_USAGE_SCHEMA:
        return {}
    protocol = str(value.get("protocol") or "")
    return {
        "schema": _CONTEXT_USAGE_SCHEMA,
        "estimated": value.get("estimated") is True,
        **{key: max(0, _safe_int(value.get(key))) for key in _CONTEXT_USAGE_TOKEN_FIELDS},
        "protocol": protocol if protocol in {"native", "text"} else "unknown",
    }


# LLM: Direct-child selection uses only root/parent/depth fields from canonical
# SubAgentTask rows. A grandchild remains visible through its own parent surface,
# not flattened into the main user's default list.
# 函数用途: 从子代理权威账中挑出当前主任务的直属子代理并生成有界展示行。
def _direct_subagent_rows(
    agent: object,
    root_task_ids: set[str],
    *,
    conversation_store: object | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    tasks, warnings = _all_subagent_tasks(agent)
    if tasks is None:
        return [], warnings

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
    rows = [
        _subagent_row(task, conversation_store=conversation_store)
        for task in selected
    ]
    return rows, warnings


# LLM: A detail page lists only exact children of the selected run and preserves
# the canonical root/depth relationship; it never flattens descendants by name.
# 函数用途: 读取一个子代理自己直接创建的下级，供递归进入详情页。
def _child_subagent_rows(
    agent: object,
    parent: object,
    *,
    conversation_store: object | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    tasks, warnings = _all_subagent_tasks(agent)
    if tasks is None:
        return [], warnings
    parent_id = str(getattr(parent, "id", "") or "").strip()
    root_id = str(getattr(parent, "root_id", "") or parent_id).strip()
    depth = max(0, _safe_int(getattr(parent, "depth", 0))) + 1
    selected = [
        task
        for task in tasks
        if str(getattr(task, "parent_id", "") or "").strip() == parent_id
        and str(getattr(task, "root_id", "") or "").strip() == root_id
        and _safe_int(getattr(task, "depth", 0)) == depth
    ]
    selected.sort(key=_subagent_sort_key)
    return [
        _subagent_row(task, conversation_store=conversation_store)
        for task in selected
    ], warnings


# LLM: One bounded report read is shared by root and nested child projections;
# load errors remain explicit warnings and never cause guessed rows.
# 函数用途: 从子代理权威管理器读取一次完整 run 名册，供精确关系过滤。
def _all_subagent_tasks(
    agent: object,
) -> tuple[list[object] | None, list[str]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return None, ["subagent_manager_unavailable"]
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
        return None, ["subagent_runs_unavailable"]
    return tasks, (["subagent_run_load_error"] if load_errors else [])


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


# LLM: This row exposes one bounded human-facing task description and numeric
# live context usage, but still omits response, tool output, paths, permissions,
# and runtime activity prose. Explicit covers ids support display joins only.
# 函数用途: 把直属子代理压成界面需要的职责短标题、状态、上下文用量与 Todo 关联。
def _subagent_row(
    task: object,
    *,
    conversation_store: object | None = None,
) -> dict[str, object]:
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
        "description": _subagent_description(task),
        "attempts": max(0, _safe_int(getattr(task, "runner_attempts", 0))),
        "context_tokens": max(0, runtime_context_token_count(task)),
        "compact_count": max(
            0,
            runtime_compact_count(task, conversation_store),
        ),
        "progress_item_ids": _progress_item_ids(task),
        "created_at": max(0.0, _safe_float(getattr(task, "created_at", 0.0))),
        "updated_at": max(0.0, _safe_float(getattr(task, "updated_at", 0.0))),
        "heartbeat_at": max(0.0, _safe_float(getattr(task, "heartbeat_at", 0.0))),
        "ended_at": max(0.0, _safe_float(getattr(task, "ended_at", 0.0))),
    }


# LLM: The child context strip reuses the frozen provider-preflight schema and
# strips every content-bearing or unknown attribute before HTTP/TUI exposure.
# 函数用途: 读取一个子代理最近一次模型调用前的完整数字上下文快照。
def _subagent_context_usage(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", None)
    usage = attrs.get("model_visible_context_usage") if isinstance(attrs, dict) else None
    return _public_context_usage(usage)


# LLM: Activity text is presentation-only and comes from typed tool/current-step
# fields with status fallbacks. It cannot decide whether the run is alive or done.
# 函数用途: 用一句短话说明当前子代理正在做什么，供详情页 Working 行显示。
def _subagent_current_activity(task: object) -> str:
    tool = _bounded_text(getattr(task, "current_tool", ""), limit=80)
    if tool:
        return f"正在使用 {tool}"
    for value in (
        getattr(task, "current_step", ""),
        getattr(task, "last_progress_summary", ""),
        getattr(task, "latest_summary", ""),
    ):
        if text := _bounded_text(value, limit=_ACTIVITY_TEXT_LIMIT):
            return text
    status = str(getattr(task, "status", "") or "").strip().upper()
    return {
        "PLANNING": "准备任务",
        "PENDING": "等待启动",
        "RUNNING": "运行中",
        "DONE": "已完成",
        "FAILED": "执行失败",
        "BLOCKED": "等待处理",
        "PAUSED": "已暂停",
        "CANCELLED": "已停止",
        "ABANDONED": "已放弃",
        "TIMEOUT": "已超时",
        "CHANNEL_ERROR": "执行通道失败",
    }.get(status, "状态未知")


# LLM: A child Todo ledger is selected only by its exact run id, which is the
# same task_local progress key used by the tool runtime. Titles remain display text.
# 函数用途: 读取子代理自己的任务清单，并隐藏与直属下级行重复的自动派工项。
def _task_progress_items_for_run(
    agent: object,
    run_id: str,
    *,
    hidden_item_ids: set[str] | None = None,
) -> tuple[tuple[dict[str, object], ...], list[str]]:
    owner_root = _owner_runtime_root(agent)
    if owner_root is None:
        return (), []
    progress, load_error = read_task_progress_report(owner_root, str(run_id or "").strip())
    if load_error:
        return (), ["task_progress_load_error"]
    raw_items = progress.get("items") if isinstance(progress, dict) else None
    if not isinstance(raw_items, list | tuple):
        return (), []
    hidden = hidden_item_ids or set()
    rows = [
        {
            "id": _bounded_text(item.get("id"), limit=128),
            "title": _bounded_text(item.get("title"), limit=_ACTIVITY_TEXT_LIMIT),
            "status": _bounded_text(item.get("status"), limit=32) or "pending",
        }
        for item in raw_items
        if isinstance(item, dict)
        and _bounded_text(item.get("id"), limit=128)
        and _bounded_text(item.get("id"), limit=128) not in hidden
    ][:_MAX_PROJECTED_PROGRESS_ITEMS]
    return tuple(rows), []


# LLM: Only the canonical child ConversationThread assistant tail can become a
# completed detail-page response. Internal runner files and result aliases are not fallbacks.
# 函数用途: 读取子代理最近一次正式模型回复，供已完成详情页显示。
def _agent_final_response(
    store: object,
    thread_id: str,
) -> tuple[str, list[str]]:
    if not thread_id:
        return "", []
    reader = getattr(store, "recent_messages_report", None)
    if not callable(reader):
        return "", ["agent_thread_unavailable"]
    try:
        messages, load_errors = reader(thread_id, limit=12)
    except Exception:
        return "", ["agent_thread_unavailable"]
    response = next(
        (
            _public_agent_text(getattr(item, "content", ""), limit=24_000)
            for item in reversed(messages)
            if str(getattr(item, "role", "") or "") == "assistant"
        ),
        "",
    )
    return response, (["agent_thread_load_error"] if load_errors else [])


# LLM: Owner-visible model prose uses the same projection and host-path
# redaction as other public transcript surfaces, then applies a display bound.
# 函数用途: 清洗子代理任务说明或最终回复，避免控制字符和宿主绝对路径进入界面。
def _public_agent_text(value: object, *, limit: int) -> str:
    content = redact_host_absolute_paths(project_user_reply(str(value or "")).content).strip()
    if len(content) <= max(1, int(limit or 1)):
        return content
    return content[: max(1, int(limit or 1)) - 1].rstrip() + "…"


# LLM: Covers are explicit task-progress ids recorded at dispatch time. They
# may drive display joins but never infer work from goal/title/summary text.
# 函数用途: 取出子代理明确负责的 Todo 项 ID，供界面原位打标。
def _progress_item_ids(task: object) -> list[str]:
    attributes = getattr(task, "attributes", None)
    covers = attributes.get("covers") if isinstance(attributes, dict) else None
    if not isinstance(covers, list | tuple):
        return []
    return list(
        dict.fromkeys(
            _bounded_text(item, limit=128)
            for item in covers[:24]
            if _bounded_text(item, limit=128)
        )
    )


# LLM: Description is display prose only. Prefer the explicit description when
# present, otherwise expose a bounded one-line copy of the creation goal; neither
# value may drive lifecycle, authorization, matching, or recovery.
# 函数用途: 选择一句能说明“这个子代理干什么”的短标题，供面板按宽度截断。
def _subagent_description(task: object) -> str:
    for field_name in ("description", "goal", "role"):
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
    "conversation_agent_view",
    "task_progress_items_for_task",
]
