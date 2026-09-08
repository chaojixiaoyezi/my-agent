"""Canonical conversation-to-subagent activity projection.

This module only reads the conversation task links and subagent run records.  It
does not start, retry, stop, or otherwise mutate an agent.  TUI, Web, and IM
surfaces can therefore share one owner-scoped display snapshot without turning
the display cache into another lifecycle authority.
"""

# LLM: This module is the read-only adapter from canonical conversation task
# 子代理结束页同步投影 canonical thread/message ID；显示层不得从正文猜最终回复身份。
# links and subagent runs to bounded public activity rows. It must never become
# a lifecycle, authorization, retry, or completion authority.
# 前台与后台 main 共用唯一数值/阶段投影，显式审批等待仍属于活动回合；历史正文和生命周期不得由此驱动。
# 模块用途: 为 TUI/Web 投影主任务与子代理状态；上下文数字独立从各自 canonical thread 读取，不制造活动状态。

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent_core.runtime.task_identity import task_path_progress_ledger_id
from ..subagents.direct_parent_lifecycle import parent_wait_blocks_dispatch
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from ..task_progress import (
    read_task_progress_report,
    task_progress_display_identity,
    task_progress_display_items,
)
from .agent_transcript import (
    agent_transcript_attempt_has_events,
    read_agent_transcript_events,
)
from .background_transcript import BackgroundTranscriptSink
from .channels import project_user_reply, redact_host_absolute_paths
from .context_usage import context_usage_from_thread
from .context_usage import public_context_usage as _public_context_usage
from .models import (
    THREAD_TASK_LINK_ACTIVE_STATUS,
    THREAD_TASK_LINK_INACTIVE_STATUSES,
)
from .tool_input_progress import public_tool_input_progress

_SCHEMA_VERSION = "conversation_agent_activity.v6"
_MAX_PROJECTED_SUBAGENTS = 64
_MAX_PROJECTED_PROGRESS_ITEMS = 128
_MAX_PROJECTED_GOALS = 16
_ACTIVITY_TEXT_LIMIT = 240
_AGENT_PROMPT_TEXT_LIMIT = 10_000
_GOAL_OBJECTIVE_TEXT_LIMIT = 4_000
_MAIN_ACTIVITY_STATE_ATTR = "_conversation_main_activity_projection"
_MAIN_ACTIVITY_LOCK_ATTR = "_conversation_main_activity_projection_lock"
_MAIN_ACTIVITY_SETUP_LOCK = threading.Lock()
_MAIN_ACTIVITY_LIVE_PHASES = frozenset(
    {
        "compacting",
        "responding",
        "retrying",
        "running",
        "thinking",
        "tool",
        "working",
        "waiting_permission",
    }
)


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
        self._publish("running", "整理任务上下文", begin=True)

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

    # LLM: 大工具参数只通过共享脱敏计数投影进入正文；main 标量活动行不
    # 保存参数内容，也不把这一阶段当成真实工具已经开始。
    # 函数用途: 显示后台主代理仍在生成工具参数，并刷新 main 活动提示。
    def write_tool_input_progress(self, value: dict[str, object]) -> bool:
        public = public_tool_input_progress(value)
        if not public:
            return False
        published = self._transcript.write_tool_input_progress(public)
        if published and str(public.get("phase") or "") != "ready":
            tool = _bounded_text(public.get("tool"), limit=80) or "工具"
            self._publish("thinking", f"正在准备 {tool} 参数")
        return published

    # LLM: Thinking text is bounded and whitespace-normalized for display; it
    # remains non-authoritative and never enters prompts or completion logic.
    # 函数用途: 把后台主代理最近一段真实思考显示在 main 行，避免用户误判卡死。
    def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> None:
        self._transcript.write_thinking(text, duration_seconds=duration_seconds)
        activity = _bounded_text(text, limit=_ACTIVITY_TEXT_LIMIT)
        self._publish("thinking", activity or "思考中")

    # LLM: 数字来自同一 provider-preflight schema，前后台共用发布函数；更新数字不改变活动阶段。
    # 函数用途: 保存后台 main 最近一次调用前的上下文总量，供所有同会话窗口读取。
    def write_context_usage(self, usage: dict[str, object]) -> bool:
        public = _public_context_usage(usage)
        if not self.thread_id or not public:
            return False
        publish_main_activity(
            self.agent, MainActivitySource(self.thread_id, self.task_id, self.started_at, self._transcript.request_id),
            context_usage=public,
        )
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
    # this callback does not write checkpoints or infer phase from display text. A superseded
    # candidate returns the scalar activity to ordinary work instead of leaving a false failure.
    # 函数用途: 把后台会话 Compact 的结构化进度送进正文进度块，并在候选放弃后恢复普通工作提示。
    def write_conversation_compact_progress(self, value: dict[str, object]) -> bool:
        published = self._transcript.write_conversation_compact_progress(value)
        if published:
            if str(value.get("phase") or "") == "superseded":
                self._publish("working", "继续当前任务")
            else:
                self._publish("compacting", "正在压缩会话上下文")
        return published

    # LLM: Finish marks only rendering phase while the durable task link remains
    # active; one model turn ending does not prove the whole task is final.
    # 函数用途: 模型轮返回后显示等待后续事件，避免有活跃 child 时误报“整理最终回复”。
    def finish(self) -> None:
        self._transcript.finish()
        self._publish("waiting", "等待后续事件")

    # LLM: Background main finalization reads only the transcript sink's typed,
    # tool-boundary-confirmed segments; scalar activity text is never promoted.
    # 函数用途: 把后台主代理已经确认的过程回复交给会话持久化层。
    def assistant_commentary_messages(self) -> tuple[str, ...]:
        return self._transcript.assistant_commentary_messages()

    # LLM: 工作片快照来自唯一 transcript sink，与活动标量无关，调用方只可随最终消息保存。
    # 函数用途: 将完整过程展示交给后台消息提交层，避免恢复时只能读到已裁剪的临时环。
    def display_history_snapshot(self) -> dict:
        return self._transcript.display_history_snapshot()

    # LLM: attempt 只给同一后台工作片的显示块命名，不能授予重试或恢复权限。
    # 函数用途: 将宿主模型执行批次传给过程 sink，保证 Compact 后工具编号不碰撞。
    def begin_model_attempt(self, attempt: int) -> None:
        self._transcript.begin_model_attempt(attempt)

    # LLM: Fail is a liveness hint only. The real exception/retry/task state is
    # still owned by the background runtime and structured lifecycle stores.
    # 函数用途: 后台主代理轮异常退出时让 main 行显示真实失败阶段。
    def fail(self) -> None:
        self._transcript.fail()
        self._publish("failed", "本轮处理失败，等待底层恢复")

    # LLM: 前后台只写同一 owner Agent 的线程级快照；不得为后台另设状态源。
    # 函数用途: 将后台真实阶段交给共用的 main 显示发布入口。
    def _publish(self, phase: str, activity: str, *, begin: bool = False) -> None:
        publish_main_activity(
            self.agent, MainActivitySource(self.thread_id, self.task_id, self.started_at, self._transcript.request_id),
            phase=phase, activity=activity, begin=begin,
        )


# LLM: 该身份只关联一个显示工作片，task/thread 来自宿主；projection_id 不能作为运行或权限依据。
# 类用途: 固定一次 main 活动更新的归属，防止旧工作片的迟到事件覆盖新工作片。
@dataclass(frozen=True)
class MainActivitySource:
    thread_id: str
    task_id: str
    started_at: float
    projection_id: str


# LLM: 这是前后台 main 标量的唯一写入点，身份由宿主提供；只保存白名单数字/短阶段。
# 换 task 不继承旧上下文；迟到的旧工作片不能覆盖新工作片。显示身份不授予执行权。
# 函数用途: 原子更新同一会话的活动与上下文，让不同 TUI 看到同一份最新数字。
def publish_main_activity(
    agent: object, source: MainActivitySource, *,
    phase: str = "", activity: str = "", context_usage: object = None, begin: bool = False,
) -> None:
    thread_id, task_id = source.thread_id, source.task_id
    if not thread_id:
        return
    with _main_activity_lock(agent):
        rows = _main_activity_rows(agent)
        current = dict(rows.get(thread_id, {}))
        if not begin and current.get("projection_id") != source.projection_id:
            return
        if current.get("task_id") != task_id:
            current = {}
        row = {
            "projection_id": source.projection_id,
            "task_id": task_id,
            "phase": phase or current.get("phase") or "running",
            "activity": _bounded_text(activity or current.get("activity"), limit=_ACTIVITY_TEXT_LIMIT),
            "started_at": source.started_at,
            "updated_at": time.time(),
        }
        usage = _public_context_usage(context_usage) or _public_context_usage(current.get("context_usage"))
        if usage:
            row["context_usage"] = usage
        rows[thread_id] = row


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
# 类用途: 汇总活跃主任务和子代理，另存不依赖任务是否活跃的最近上下文展示。
@dataclass(frozen=True)
class ConversationAgentActivity:
    active_task_count: int = 0
    compact_count: int = 0
    main_activity: dict[str, object] | None = None
    context_usage: dict[str, object] | None = None
    context_usage_projection_ok: bool = True
    goals: tuple[dict[str, object], ...] = ()
    subagents: tuple[dict[str, object], ...] = ()
    task_progress_items: tuple[dict[str, object], ...] = ()
    task_progress_generation_id: str = ""
    task_progress_plan_revision: int = 0
    hidden_subagent_count: int = 0
    active_task_projection_ok: bool = True
    goal_projection_ok: bool = True
    subagent_projection_ok: bool = True
    task_progress_projection_ok: bool = True
    warnings: tuple[str, ...] = ()

    # LLM: JSON output contains only bounded public fields; canonical task/run
    # records and load errors remain in their existing stores.
    # 函数用途: 生成 Gateway 有界投影，空上下文表示没有本代快照，不表示上下文被删除。
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "active_task_count": max(0, int(self.active_task_count or 0)),
            "compact_count": max(0, int(self.compact_count or 0)),
            "main_activity": dict(self.main_activity or {}),
            "context_usage": dict(self.context_usage or {}),
            "context_usage_projection_ok": self.context_usage_projection_ok,
            "goals": [dict(row) for row in self.goals],
            "subagents": [dict(row) for row in self.subagents],
            "task_progress_items": [dict(row) for row in self.task_progress_items],
            "task_progress_generation_id": str(self.task_progress_generation_id or ""),
            "task_progress_plan_revision": max(
                0,
                int(self.task_progress_plan_revision or 0),
            ),
            "hidden_subagent_count": max(0, int(self.hidden_subagent_count or 0)),
            "active_task_projection_ok": bool(self.active_task_projection_ok),
            "goal_projection_ok": bool(self.goal_projection_ok),
            "subagent_projection_ok": bool(self.subagent_projection_ok),
            "task_progress_projection_ok": bool(self.task_progress_projection_ok),
            "subagent_projection_warnings": list(self.warnings),
        }


# LLM: This is the single read adapter from conversation task identity to direct
# child run activity. It must not infer roots from prompt text or expose descendants
# outside the active, already-authenticated thread.
# 函数用途: 读取会话及直属子代理的状态，空闲也返回同代上下文数字，不启动或延长任务。
def conversation_agent_activity(
    agent: object,
    store: object,
    thread_id: str,
) -> ConversationAgentActivity:
    compact_count, context_usage, compact_warnings = _conversation_context_state(store, thread_id)
    goals, goal_warnings = _conversation_goal_rows(store, thread_id)
    active_links, link_warnings = _active_task_links(store, thread_id)
    workspace_task_id = _conversation_workspace_task_id(store, thread_id)
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
            context_usage=context_usage,
            context_usage_projection_ok=not compact_warnings,
            goals=tuple(goals),
            active_task_projection_ok=not link_warnings,
            goal_projection_ok=not goal_warnings,
            warnings=tuple(
                dict.fromkeys((*link_warnings, *goal_warnings, *compact_warnings))
            ),
        )

    rows, run_warnings = _direct_subagent_rows(
        agent,
        set(display_task_ids),
        conversation_store=store,
    )
    main_activity = _conversation_main_activity(
        agent,
        thread_id,
        active_links,
        workspace_task_id=workspace_task_id,
    )
    live_task_ids, execution_warnings = _live_activity_task_ids(
        agent,
        store,
        thread_id,
        active_links,
        rows,
        main_activity,
        subagent_projection_ok=not run_warnings,
    )
    (
        progress_items,
        progress_generation_id,
        progress_plan_revision,
        progress_warnings,
    ) = _task_progress_items_from_links(
        agent,
        display_links,
        preferred_task_id=_conversation_workspace_task_id(store, thread_id),
        hidden_item_ids=_row_run_ids(rows),
    )
    # 已结束任务仍保留子代理名册供进入查看，但不再把模型未勾选的旧 Todo 画成“正在做”。
    # 空快照沿用同一 generation/revision，TUI 可精确收起面板而不改写持久账本。
    if display_links and all(_task_link_is_inactive(link) for link in display_links):
        progress_items = ()
    visible = rows[:_MAX_PROJECTED_SUBAGENTS]
    return ConversationAgentActivity(
        active_task_count=len(live_task_ids),
        compact_count=compact_count,
        context_usage=context_usage,
        context_usage_projection_ok=not compact_warnings,
        main_activity=main_activity,
        goals=tuple(goals),
        subagents=tuple(visible),
        task_progress_items=progress_items,
        task_progress_generation_id=progress_generation_id,
        task_progress_plan_revision=progress_plan_revision,
        hidden_subagent_count=max(0, len(rows) - len(visible)),
        active_task_projection_ok=not link_warnings,
        goal_projection_ok=not goal_warnings,
        subagent_projection_ok=not run_warnings,
        task_progress_projection_ok=not progress_warnings,
        warnings=tuple(
            dict.fromkeys(
                (
                    *link_warnings,
                    *goal_warnings,
                    *run_warnings,
                    *execution_warnings,
                    *progress_warnings,
                    *compact_warnings,
                )
            )
        ),
    )


# LLM: Goal rows are a bounded owner-facing projection of exact ThreadGoal
# records. They are display data only: the TUI must never use these rows to
# schedule, pause, resume, or complete a goal.
# 函数用途: 读取当前会话尚未完成的 Goal，供底部状态行和只读详情展示。
def _conversation_goal_rows(
    store: object,
    thread_id: str,
) -> tuple[list[dict[str, object]], list[str]]:
    loader = getattr(store, "load_goals_report", None)
    if not callable(loader):
        # Embedded/legacy read adapters may intentionally omit Goal support;
        # absence means no projection, while an available loader that fails is
        # a real authoritative-read warning and must retain the prior TUI row.
        return [], []
    try:
        raw_goals, load_error = loader(str(thread_id or ""))
    except Exception:
        return [], ["conversation_goal_projection_unavailable"]
    if load_error:
        return [], ["conversation_goal_projection_load_error"]
    elapsed_reader = getattr(store, "current_goal_time_seconds", None)
    rows: list[dict[str, object]] = []
    for goal in list(raw_goals or []):
        status = str(getattr(goal, "status", "") or "").strip().lower()
        goal_id = str(getattr(goal, "goal_id", "") or "").strip()
        if not goal_id or status == "complete":
            continue
        try:
            elapsed = (
                float(elapsed_reader(goal))
                if callable(elapsed_reader)
                else float(getattr(goal, "time_used_seconds", 0.0) or 0.0)
            )
        except Exception:
            elapsed = float(getattr(goal, "time_used_seconds", 0.0) or 0.0)
        token_budget = getattr(goal, "token_budget", None)
        duration_seconds = getattr(goal, "duration_seconds", None)
        row: dict[str, object] = {
            "goal_id": goal_id,
            "name": _bounded_text(getattr(goal, "name", ""), limit=240),
            "objective": _bounded_text(
                getattr(goal, "objective", ""),
                limit=_GOAL_OBJECTIVE_TEXT_LIMIT,
            ),
            "status": status,
            "tokens_used": max(0, int(getattr(goal, "tokens_used", 0) or 0)),
            "time_used_seconds": max(0, int(elapsed)),
            "created_at": max(0.0, _safe_float(getattr(goal, "created_at", 0.0))),
            "updated_at": max(0.0, _safe_float(getattr(goal, "updated_at", 0.0))),
        }
        if token_budget is not None:
            row["token_budget"] = max(1, int(token_budget))
        if duration_seconds is not None:
            row["duration_seconds"] = max(1, int(duration_seconds))
        rows.append(row)
    rows.sort(key=lambda item: (float(item.get("created_at") or 0.0), str(item["goal_id"])))
    return rows[:_MAX_PROJECTED_GOALS], []


# LLM: TUI liveness follows the active-turn boundary, not the resumable
# ThreadTaskLink index. Exact thread claims/policies, nonterminal direct children,
# and a volatile live main phase may keep a row active; a waiting row plus a fully
# terminal child roster must become idle without mutating the durable task link.
# 函数用途: 判断哪些主任务此刻真有执行器或未结束子代理，避免任务记录仍 active 时底部永久闪 Working。
def _live_activity_task_ids(
    agent: object,
    store: object,
    thread_id: str,
    active_links: list[object],
    rows: list[dict[str, object]],
    main_activity: dict[str, object],
    *,
    subagent_projection_ok: bool,
) -> tuple[set[str], list[str]]:
    del agent
    active_ids = {
        str(getattr(link, "task_id", "") or "").strip()
        for link in active_links
        if str(getattr(link, "task_id", "") or "").strip()
    }
    if not active_ids:
        return set(), []

    warnings: list[str] = []
    execution_ids: set[str] = set()
    execution_state_known = False
    execution_reader_available = callable(
        getattr(store, "list_progress_policies_report", None)
    ) and callable(getattr(store, "load_background_run_claim_report", None))
    try:
        if not execution_reader_available:
            raise AttributeError("conversation execution readers unavailable")
        from .task_promotion import conversation_thread_execution_state

        state = conversation_thread_execution_state(store, thread_id)
        execution_state_known = state.get("state_available") is True
        raw_running = state.get("running_task_ids")
        if isinstance(raw_running, list | tuple | set):
            execution_ids.update(
                str(item).strip() for item in raw_running if str(item).strip()
            )
        if execution_reader_available and not execution_state_known:
            warnings.append("conversation_execution_state_unavailable")
    except Exception:
        if execution_reader_available:
            warnings.append("conversation_execution_state_unavailable")

    live_ids = active_ids & execution_ids
    for row in rows:
        status = str(row.get("status") or "").strip().upper()
        if task_status_in(status, SUBAGENT_ENDED_STATUSES):
            continue
        root_task_id = str(row.get("root_task_id") or "").strip()
        if root_task_id in active_ids:
            live_ids.add(root_task_id)

    main_task_id = str(main_activity.get("task_id") or "").strip()
    main_phase = str(main_activity.get("phase") or "").strip().lower()
    if main_task_id in active_ids and main_phase in _MAIN_ACTIVITY_LIVE_PHASES:
        live_ids.add(main_task_id)

    # A broken child or execution projection must not falsely announce idle.
    # Real ConversationStore reads are exact; lightweight test/legacy adapters
    # intentionally keep the previous conservative behavior.
    if not execution_state_known or not subagent_projection_ok:
        live_ids.update(active_ids)
    return live_ids, warnings


# LLM: The main row is bound to ConversationThread.workspace_task_id, which is the
# current user turn's canonical root. Child links and an older volatile sink row may
# never replace its identity or clock; the renderer receives no inferred prose timing.
# 函数用途: 组合当前主任务的实时阶段与持久起点，让新回合和 Gateway 重启后计时仍然正确。
def _conversation_main_activity(
    agent: object,
    thread_id: str,
    active_links: list[object],
    *,
    workspace_task_id: str,
) -> dict[str, object]:
    active_by_id = {
        str(getattr(link, "task_id", "") or "").strip(): link
        for link in active_links
        if str(getattr(link, "task_id", "") or "").strip()
    }
    current_link = active_by_id.get(str(workspace_task_id or "").strip())
    allowed_task_ids = (
        {str(workspace_task_id).strip()}
        if current_link is not None
        else set(active_by_id)
    )
    if not allowed_task_ids:
        return {}
    row = background_main_activity(agent, thread_id, allowed_task_ids)
    if current_link is None:
        return row
    link_started_at = max(
        0.0,
        _safe_float(getattr(current_link, "created_at", 0.0)),
    )
    started_at = link_started_at or max(0.0, _safe_float(row.get("started_at")))
    return {
        "task_id": str(workspace_task_id).strip(),
        "phase": _bounded_text(row.get("phase"), limit=40) or "waiting",
        "activity": (
            _bounded_text(row.get("activity"), limit=_ACTIVITY_TEXT_LIMIT)
            or "等待后续事件"
        ),
        "started_at": started_at,
        "updated_at": max(started_at, _safe_float(row.get("updated_at"))),
        **(
            {"context_usage": dict(row["context_usage"])}
            if isinstance(row.get("context_usage"), dict)
            else {}
        ),
    }


# LLM: Child detail is a bounded owner-facing projection over one exact canonical
# run. The exact delegated goal is the first user-message source; the roster-only
# description must never replace it. This reader cannot authorize or mutate a run.
# 函数用途: 返回子代理详情页的完整派工要求、状态、直属下级、上下文、过程事件和最终回复。
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
    context_state = _subagent_context_state(task, store)
    row = _subagent_row(agent, task, conversation_store=store, context_state=context_state)
    rows, warnings = _child_subagent_rows(
        agent,
        task,
        conversation_store=store,
    )
    (
        progress_items,
        progress_generation_id,
        progress_plan_revision,
        progress_warnings,
    ) = _task_progress_items_for_run(
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
    (
        final_response,
        final_response_request_id,
        final_response_message_id,
        history_warnings,
    ) = _agent_final_response(store, thread_id)
    status = str(getattr(task, "status", "") or "").strip().upper()
    terminal = task_status_in(status, SUBAGENT_ENDED_STATUSES)
    transcript_warnings = (
        ["agent_transcript_load_error"] if transcript.get("load_errors") else []
    )
    return {
        "schema_version": "conversation_agent_view.v2",
        "ok": True,
        "thread_id": thread_id,
        "agent": {
            **row,
            "goal": _public_agent_text(
                getattr(task, "goal", "") or getattr(task, "description", ""),
                limit=_AGENT_PROMPT_TEXT_LIMIT,
            ),
            "activity": _subagent_current_activity(
                task,
                lifecycle_phase=str(row.get("lifecycle_phase") or ""),
            ),
            "context_usage": context_state[1],
        },
        "terminal": terminal,
        "children": rows[:_MAX_PROJECTED_SUBAGENTS],
        "hidden_child_count": max(0, len(rows) - _MAX_PROJECTED_SUBAGENTS),
        "task_progress_items": list(progress_items),
        "task_progress_generation_id": progress_generation_id,
        "task_progress_plan_revision": progress_plan_revision,
        "transcript_events": list(transcript.get("events") or []),
        "event_cursor": max(0, _safe_int(transcript.get("cursor"))),
        "events_truncated": bool(transcript.get("truncated")),
        # 运行中的历史 assistant 段可能只是 Compact 前一轮，不能冒充当前任务 final。
        "final_response": final_response if terminal else "",
        "final_response_message_id": final_response_message_id if terminal else "",
        # typed transcript 和兜底 final 都指向同一个模型 request；TUI 必须按
        # 这个结构化身份归并，不能拿自然语言正文猜是不是重复。
        "final_response_request_id": (
            f"bg-agent:{selected}:{final_response_request_id}"
            if terminal and final_response and final_response_request_id
            else ""
        ),
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


# LLM: generation 与数值从同一 thread 读取，旧代快照不得配上新代数；不从累计用量或活动文字推断。
# 函数用途: 一次读取会话压缩次数及其最近上下文数字，空闲和重启后使用同一权威记录。
def _conversation_context_state(
    store: object,
    thread_id: str,
) -> tuple[int, dict[str, object], list[str]]:
    loader = getattr(store, "load_thread_report", None)
    if not callable(loader):
        return 0, {}, []
    try:
        thread, load_error = loader(thread_id)
    except Exception:
        return 0, {}, ["conversation_thread_unavailable"]
    count = max(0, _safe_int(getattr(thread, "compact_generation", 0)))
    warnings = ["conversation_thread_load_error"] if load_error else []
    return count, context_usage_from_thread(thread) if not load_error else {}, warnings


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
    items, _generation_id, _plan_revision = task_progress_projection_for_task(
        agent,
        store,
        task_id,
    )
    return items


# LLM: Final notices need the same rows and opaque display identity as live
# activity polling. This adapter exposes no lifecycle authority and delegates to
# the exact task-link/task-path projection used by conversation_agent_activity.
# 函数用途: 读取后台最终回复对应的 Todo 快照、回合标识和修订号。
def task_progress_projection_for_task(
    agent: object,
    store: object,
    task_id: str,
) -> tuple[tuple[dict[str, object], ...], str, int]:
    loader = getattr(store, "load_task_link", None)
    if not callable(loader) or not str(task_id or "").strip():
        return (), "", 0
    try:
        link = loader(str(task_id).strip())
    except Exception:
        return (), "", 0
    rows, _run_warnings = _direct_subagent_rows(
        agent,
        {str(task_id).strip()},
        conversation_store=store,
    )
    items, generation_id, plan_revision, _warnings = _task_progress_items_from_links(
        agent,
        [link] if link else [],
        hidden_item_ids=_row_run_ids(rows),
    )
    if _task_link_is_inactive(link):
        return (), generation_id, plan_revision
    return items, generation_id, plan_revision


# LLM: Todo visibility follows only the typed ConversationTaskLink lifecycle. This presentation
# helper never closes ledger rows, accepts task quality, or infers completion from assistant prose.
# 函数用途: 判断任务链接是否已结束，以便终态仍保留子代理名册但收起误导性的活动清单。
def _task_link_is_inactive(link: object) -> bool:
    return (
        str(getattr(link, "status", "") or "").strip().lower()
        in THREAD_TASK_LINK_INACTIVE_STATUSES
    )


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
) -> tuple[tuple[dict[str, object], ...], str, int, list[str]]:
    candidates = [link for link in links if link is not None]
    if not candidates:
        return (), "", 0, []
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
        return (), "", 0, []
    ledger_id = task_path_progress_ledger_id(task_path)
    progress, load_error = read_task_progress_report(owner_root, ledger_id)
    if load_error:
        return (), "", 0, ["task_progress_load_error"]
    raw_items = task_progress_display_items(progress) if isinstance(progress, dict) else None
    if not isinstance(raw_items, list | tuple):
        return (), "", 0, []
    generation_id, plan_revision = task_progress_display_identity(progress)
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
    return tuple(rows), generation_id, plan_revision, []


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
    tasks, warnings = _related_subagent_tasks(
        agent,
        root_task_ids=root_task_ids,
        parent_run_ids=root_task_ids,
        depth=1,
    )
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
        _subagent_row(agent, task, conversation_store=conversation_store)
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
    parent_id = str(getattr(parent, "id", "") or "").strip()
    root_id = str(getattr(parent, "root_id", "") or parent_id).strip()
    depth = max(0, _safe_int(getattr(parent, "depth", 0))) + 1
    tasks, warnings = _related_subagent_tasks(
        agent,
        root_task_ids={root_id},
        parent_run_ids={parent_id},
        depth=depth,
    )
    if tasks is None:
        return [], warnings
    selected = [
        task
        for task in tasks
        if str(getattr(task, "parent_id", "") or "").strip() == parent_id
        and str(getattr(task, "root_id", "") or "").strip() == root_id
        and _safe_int(getattr(task, "depth", 0)) == depth
    ]
    selected.sort(key=_subagent_sort_key)
    return [
        _subagent_row(agent, task, conversation_store=conversation_store)
        for task in selected
    ], warnings


# LLM: The SQLite control plane is only a bounded lookup index. It selects exact run ids for the
# requested root/parent/depth relation, then the persistence service reloads those ids from
# canonical task files. If either adapter is unavailable, legacy/fake agents retain the full-scan
# compatibility path; prompt text, titles, and status prose never participate in selection.
# 函数用途: 先按结构化父子关系查索引，再只读取命中的子代理，避免每个 TUI 快照复制全部历史 run。
def _related_subagent_tasks(
    agent: object,
    *,
    root_task_ids: set[str],
    parent_run_ids: set[str],
    depth: int,
) -> tuple[list[object] | None, list[str]]:
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return None, ["subagent_manager_unavailable"]
    local_store = getattr(manager, "local_store", None)
    tree_reader = getattr(local_store, "list_agent_tree", None)
    selected_reader = getattr(manager, "list_runs_by_ids_report", None)
    if callable(tree_reader) and callable(selected_reader):
        try:
            selected_ids = _indexed_related_run_ids(
                tree_reader,
                root_task_ids=root_task_ids,
                parent_run_ids=parent_run_ids,
                depth=depth,
            )
            report = selected_reader(selected_ids)
            tasks = list(getattr(report, "runs", ()) or ())
            load_errors = list(getattr(report, "load_errors", ()) or ())
            return tasks, (["subagent_run_load_error"] if load_errors else [])
        except Exception:
            # Canonical full-scan fallback preserves existing/fake deployments when the optional
            # lookup projection is unavailable; it is not a second result authority.
            pass
    return _all_subagent_tasks(agent)


# LLM: This helper reads each requested root through the bounded SQLite projection and delegates
# per-record structural filtering. It returns stable unique ids only; canonical task reads happen
# afterward and remain authoritative.
# 函数用途: 从一个或多个根任务索引中收集符合父级和层级条件的唯一子代理 id。
def _indexed_related_run_ids(
    tree_reader: object,
    *,
    root_task_ids: set[str],
    parent_run_ids: set[str],
    depth: int,
) -> list[str]:
    selected: list[str] = []
    for root_id in sorted(root_task_ids):
        tree = tree_reader(root_id)
        selected.extend(
            _indexed_run_ids_for_root(
                tree,
                root_id=root_id,
                parent_run_ids=parent_run_ids,
                depth=depth,
            )
        )
    return list(dict.fromkeys(selected))


# LLM: Index records are matched only by typed root, parent, depth, and run id fields. Display
# names, task prose, status labels, and paths must never select a child relation.
# 函数用途: 在单个根任务的索引结果中筛出指定直属层级的 run id。
def _indexed_run_ids_for_root(
    tree: object,
    *,
    root_id: str,
    parent_run_ids: set[str],
    depth: int,
) -> list[str]:
    selected: list[str] = []
    for record in list(getattr(tree, "runs", ()) or ()):
        if str(getattr(record, "root_task_id", "") or "").strip() != root_id:
            continue
        parent_id = str(getattr(record, "parent_run_id", "") or "").strip()
        if parent_id not in parent_run_ids:
            continue
        if _safe_int(getattr(record, "depth", 0)) != depth:
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        if run_id:
            selected.append(run_id)
    return selected


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
# 函数用途: 生成子代理职责和状态；上下文与 Compact 次数使用同一次 thread 读取，避免混代。
def _subagent_row(
    agent: object,
    task: object,
    *,
    conversation_store: object | None = None,
    context_state: tuple[int, dict[str, object], list[str]] | None = None,
) -> dict[str, object]:
    compact_count, context_usage, _ = context_state if context_state is not None else _subagent_context_state(task, conversation_store)
    status = str(getattr(task, "status", "") or "").strip().upper()
    lifecycle_phase = _subagent_lifecycle_phase(agent, task, status=status)
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
        "lifecycle_phase": lifecycle_phase,
        "description": _subagent_description(task),
        "attempts": max(0, _safe_int(getattr(task, "runner_attempts", 0))),
        "context_tokens": context_usage.get("current_tokens", 0),
        "context_known": bool(context_usage),
        "compact_count": compact_count,
        "progress_item_ids": _progress_item_ids(task),
        "created_at": max(0.0, _safe_float(getattr(task, "created_at", 0.0))),
        "updated_at": max(0.0, _safe_float(getattr(task, "updated_at", 0.0))),
        "heartbeat_at": max(0.0, _safe_float(getattr(task, "heartbeat_at", 0.0))),
        "ended_at": max(0.0, _safe_float(getattr(task, "ended_at", 0.0))),
    }


# LLM: 子页和名册从同一 agent_thread_id 一次读取代次与数字；旧 run 属性不作为回退事实源。
# 函数用途: 读取子代理最近上下文及其压缩代次，成功压缩后旧快照不可复活。
def _subagent_context_state(task: object, store: object | None) -> tuple[int, dict[str, object], list[str]]:
    thread_id = str(getattr(task, "agent_thread_id", "") or "").strip()
    if not thread_id:
        return 0, {}, []
    return _conversation_context_state(store, thread_id)


# LLM: Activity text is presentation-only and comes from typed tool/current-step
# fields with status fallbacks. It cannot decide whether the run is alive or done.
# 函数用途: 用一句短话说明当前子代理正在做什么，供详情页 Working 行显示。
def _subagent_current_activity(
    task: object,
    *,
    lifecycle_phase: str = "",
) -> str:
    status = str(getattr(task, "status", "") or "").strip().upper()
    terminal_activity = {
        "DONE": "已完成",
        "FAILED": "执行失败",
        "BLOCKED": "等待处理",
        "PAUSED": "已暂停",
        "CANCELLED": "已停止",
        "ABANDONED": "已放弃",
        "TIMEOUT": "已超时",
        "CHANNEL_ERROR": "执行通道失败",
        "TAKEN_OVER": "已被接管",
    }.get(status)
    if terminal_activity:
        return terminal_activity
    startup_activity = {
        "queued": "等待调度",
        "starting": "正在启动执行器",
        "waiting_first_event": "等待模型首个响应",
        "waiting_descendants": "等待直属子代理完成",
    }.get(str(lifecycle_phase or "").strip().lower())
    if startup_activity:
        return startup_activity
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
    return {
        "PLANNING": "准备任务",
        "PENDING": "等待启动",
        "RUNNING": "运行中",
    }.get(status, "状态未知")


# LLM: This presentation phase distinguishes initialization, active turns and child waiting using only
# canonical task status, the durable direct-child wait marker, and the exact active-attempt public
# event stream. It never uses elapsed time, prose, or context-token estimates and cannot schedule.
# 函数用途: 区分子代理排队、启动、等待模型首事件、等待直属下级和真正运行，供 TUI/Web 如实显示。
def _subagent_lifecycle_phase(
    agent: object,
    task: object,
    *,
    status: str,
) -> str:
    if status == "PLANNING":
        return "queued"
    if status == "PENDING":
        if parent_wait_blocks_dispatch(task):
            return "waiting_descendants"
        return "starting"
    if status == "RUNNING":
        run_id = str(getattr(task, "id", "") or "").strip()
        attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
        if run_id and attempt_id:
            try:
                if not agent_transcript_attempt_has_events(
                    agent,
                    run_id=run_id,
                    attempt_id=attempt_id,
                ):
                    return "waiting_first_event"
            except (OSError, RuntimeError, TypeError, ValueError):
                return "waiting_first_event"
        return "running"
    if status == "BLOCKED":
        return "waiting_input"
    if status == "PAUSED":
        return "paused"
    if task_status_in(status, SUBAGENT_ENDED_STATUSES):
        return "terminal"
    return "unknown"


# LLM: A child Todo ledger is selected only by its exact run id, which is the
# same task_local progress key used by the tool runtime. Titles remain display text.
# 函数用途: 读取子代理自己的任务清单，并隐藏与直属下级行重复的自动派工项。
def _task_progress_items_for_run(
    agent: object,
    run_id: str,
    *,
    hidden_item_ids: set[str] | None = None,
) -> tuple[tuple[dict[str, object], ...], str, int, list[str]]:
    owner_root = _owner_runtime_root(agent)
    if owner_root is None:
        return (), "", 0, []
    progress, load_error = read_task_progress_report(owner_root, str(run_id or "").strip())
    if load_error:
        return (), "", 0, ["task_progress_load_error"]
    raw_items = task_progress_display_items(progress) if isinstance(progress, dict) else None
    if not isinstance(raw_items, list | tuple):
        return (), "", 0, []
    generation_id, plan_revision = task_progress_display_identity(progress)
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
    return tuple(rows), generation_id, plan_revision, []


# LLM: Only the canonical child ConversationThread assistant tail can become a
# completed detail-page response. Return its canonical message id and conversation request id
# with the text so typed transcript and history fallback can be joined without
# natural-language dedupe; internal runner files and result aliases are not fallbacks.
# 函数用途: 读取子代理正式回复、消息 ID 和回合标识；不能用正文内容代替去重身份。
def _agent_final_response(
    store: object,
    thread_id: str,
) -> tuple[str, str, str, list[str]]:
    if not thread_id:
        return "", "", "", []
    reader = getattr(store, "recent_messages_report", None)
    if not callable(reader):
        return "", "", "", ["agent_thread_unavailable"]
    try:
        messages, load_errors = reader(thread_id, limit=12)
    except Exception:
        return "", "", "", ["agent_thread_unavailable"]
    message = next(
        (
            item
            for item in reversed(messages)
            if str(getattr(item, "role", "") or "") == "assistant"
        ),
        None,
    )
    if message is None:
        return "", "", "", (["agent_thread_load_error"] if load_errors else [])
    response = _public_agent_text(getattr(message, "content", ""), limit=24_000)
    metadata = getattr(message, "metadata", None)
    request_id = (
        str(metadata.get("conversation_request_id") or "").strip()
        if isinstance(metadata, dict)
        else ""
    )
    return (
        response,
        request_id,
        str(getattr(message, "message_id", "") or ""),
        (["agent_thread_load_error"] if load_errors else []),
    )


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
    "task_progress_projection_for_task",
]
