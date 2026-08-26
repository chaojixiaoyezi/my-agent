"""Typed navigation state for entering and controlling delegated-agent views."""

# LLM: This module owns only TUI selection, view stack, per-view display runtimes,
# and event cursors. Canonical run status, messages, guidance, cancellation, and
# authorization remain on Gateway/domain services and are never inferred here.
# 模块用途: 支持空输入时方向键选择子代理、Enter 进入、Ctrl+G 返回，并缓存每个代理的独立展示页面。

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .tui_runtime import TuiRuntime

_TERMINAL_AGENT_STATUSES = frozenset(
    {
        "DONE",
        "FAILED",
        "BLOCKED",
        "CHANNEL_ERROR",
        "TIMEOUT",
        "CANCELLED",
        "ABANDONED",
        "TAKEN_OVER",
    }
)
_ROW_SCALAR_FIELDS = frozenset(
    {
        "run_id",
        "root_task_id",
        "parent_run_id",
        "depth",
        "name",
        "role",
        "status",
        "description",
        "goal",
        "activity",
        "attempts",
        "context_tokens",
        "compact_count",
        "created_at",
        "updated_at",
        "heartbeat_at",
        "ended_at",
    }
)


# LLM: The snapshot is the renderer/keybinding boundary. It contains only exact
# run ids and bounded display scalars, never task objects or control authority.
# 类用途: 告诉界面当前看的是谁、选中了谁、能否继续输入或停止。
@dataclass(frozen=True)
class TuiAgentNavigationSnapshot:
    active_run_id: str = ""
    active_name: str = "main"
    active_status: str = ""
    selected_run_id: str = ""
    depth: int = 0
    terminal: bool = False


# LLM: One navigation instance belongs to one TUI application and one root
# runtime. Child runtimes are display stores only and cannot execute model work.
# 类用途: 管理当前代理视图栈、每层子代理名册、方向键选中项和独立正文页面。
class TuiAgentNavigationState:
    # LLM: Construction fixes the root runtime for the application lifetime;
    # changing sessions requires a new navigation instance.
    # 函数用途: 创建一个从主代理开始的空导航栈。
    def __init__(self, root_runtime: TuiRuntime) -> None:
        if not isinstance(root_runtime, TuiRuntime):
            raise TypeError("root_runtime must be TuiRuntime")
        self.root_runtime = root_runtime
        self._runtimes: dict[str, TuiRuntime] = {}
        self._rows_by_parent: dict[str, tuple[dict[str, object], ...]] = {"": ()}
        self._rows_by_run: dict[str, dict[str, object]] = {}
        self._selected_by_parent: dict[str, str] = {}
        self._path: list[str] = []
        self._event_cursors: dict[str, int] = {}
        self._goal_published: set[str] = set()
        self._final_responses: dict[str, str] = {}
        self._view_change: Callable[[TuiRuntime], None] | None = None
        self._lock = threading.RLock()

    # LLM: The callback swaps only the visible state store/provider. It is
    # invoked outside the navigation lock to avoid UI re-entry deadlocks.
    # 函数用途: 绑定进入或返回代理时的界面切换动作。
    def set_view_change_callback(
        self,
        callback: Callable[[TuiRuntime], None] | None,
    ) -> None:
        with self._lock:
            self._view_change = callback

    # LLM: Root and child pollers replace one exact parent's row projection.
    # Selection follows run identity across reorder and clears if the target vanished.
    # 函数用途: 更新某一层直属子代理名册，并保持当前选中项稳定。
    def update_rows(self, parent_run_id: str, value: object) -> bool:
        parent = str(parent_run_id or "").strip()
        rows = _navigation_rows(value, parent_run_id=parent)
        with self._lock:
            previous = self._rows_by_parent.get(parent, ())
            if previous == rows:
                return False
            self._rows_by_parent[parent] = rows
            for row in rows:
                self._rows_by_run[str(row["run_id"])] = row
            selected = self._selected_by_parent.get(parent, "")
            if selected and all(str(row["run_id"]) != selected for row in rows):
                self._selected_by_parent.pop(parent, None)
            runtime = self._runtimes.get(parent) if parent else self.root_runtime
        if runtime is not None:
            runtime.store.invalidate()
        return True

    # LLM: Direction movement is active only when the input controller explicitly
    # calls it. It never consumes history or changes the current view itself.
    # 函数用途: 在当前层的子代理行之间移动选择；首次向下选第一行，首次向上选最后一行。
    def move_selection(self, delta: int) -> bool:
        step = int(delta or 0)
        if not step:
            return False
        with self._lock:
            parent = self._active_run_id_locked()
            rows = self._rows_by_parent.get(parent, ())
            if not rows:
                return False
            ids = [str(row["run_id"]) for row in rows]
            selected = self._selected_by_parent.get(parent, "")
            if selected not in ids:
                index = 0 if step > 0 else len(ids) - 1
            else:
                index = max(0, min(len(ids) - 1, ids.index(selected) + step))
            self._selected_by_parent[parent] = ids[index]
            runtime = self._active_runtime_locked()
        runtime.store.invalidate()
        return True

    # LLM: Enter follows the selected exact run id from the current parent's
    # authenticated row set. A transient loading notice is display-only; it
    # cannot enter a typed id supplied from input text or imply run liveness.
    # 函数用途: 进入当前选中的子代理详情页，详细快照到达前显示载入提示。
    def enter_selected(self) -> bool:
        with self._lock:
            parent = self._active_run_id_locked()
            selected = self._selected_by_parent.get(parent, "")
            if not selected or selected not in {
                str(row["run_id"]) for row in self._rows_by_parent.get(parent, ())
            }:
                return False
            self._path.append(selected)
            runtime = self._runtime_for_locked(selected)
            callback = self._view_change
            has_snapshot = selected in self._goal_published
        if not has_snapshot:
            runtime.set_notice("正在载入子代理详情…", duration_seconds=1.5)
        if callback is not None:
            callback(runtime)
        runtime.store.invalidate()
        return True

    # LLM: Back pops exactly one visual parent. It does not cancel, pause, resume,
    # or mutate either agent, which is why Esc is deliberately not routed here.
    # 函数用途: 用 Ctrl+G、Alt+左箭头或 `/back` 返回上一层代理视图。
    def back(self) -> bool:
        with self._lock:
            if not self._path:
                return False
            self._path.pop()
            runtime = self._active_runtime_locked()
            callback = self._view_change
        if callback is not None:
            callback(runtime)
        runtime.store.invalidate()
        return True

    # LLM: Typing clears only the current visual selection; the focused child
    # view and every canonical run remain unchanged.
    # 函数用途: 用户开始输入正文时取消方向键高亮，避免误以为 Enter 会继续进入下级。
    def clear_selection(self) -> bool:
        with self._lock:
            parent = self._active_run_id_locked()
            if parent not in self._selected_by_parent:
                return False
            self._selected_by_parent.pop(parent, None)
            runtime = self._active_runtime_locked()
        runtime.store.invalidate()
        return True

    # LLM: Snapshot reads one immutable display boundary and derives terminal
    # only from the canonical status scalar supplied by the backend.
    # 函数用途: 返回渲染器和按键层需要的当前代理导航状态。
    def snapshot(self) -> TuiAgentNavigationSnapshot:
        with self._lock:
            active = self._active_run_id_locked()
            row = dict(self._rows_by_run.get(active, {})) if active else {}
            status = str(row.get("status") or "").strip().upper()
            return TuiAgentNavigationSnapshot(
                active_run_id=active,
                active_name=str(row.get("name") or row.get("role") or active or "main"),
                active_status=status,
                selected_run_id=self._selected_by_parent.get(active, ""),
                depth=len(self._path),
                terminal=bool(active and status in _TERMINAL_AGENT_STATUSES),
            )

    # LLM: The active runtime is a display store selected by the view stack; it
    # cannot be used as a task identity or control capability.
    # 函数用途: 返回当前页面应渲染和显示提示的 TuiRuntime。
    def active_runtime(self) -> TuiRuntime:
        with self._lock:
            return self._active_runtime_locked()

    # LLM: Event cursors are per exact run so switching between children never
    # skips or duplicates another child's process stream.
    # 函数用途: 返回指定子代理已经消费的过程事件游标。
    def event_cursor(self, run_id: str) -> int:
        with self._lock:
            return max(0, int(self._event_cursors.get(str(run_id or ""), 0)))

    # LLM: One authenticated detail payload updates only its exact run runtime.
    # The first non-empty delegated goal becomes the child page's user block;
    # a transient incomplete payload must not permanently suppress that prompt.
    # 函数用途: 把 Gateway 子代理完整消息快照应用到对应页面并增量刷新正文。
    def apply_agent_view(self, run_id: str, payload: object) -> bool:
        selected = str(run_id or "").strip()
        if not selected or not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return False
        raw_agent = payload.get("agent")
        if not isinstance(raw_agent, Mapping):
            return False
        row = _navigation_row(raw_agent, expected_run_id=selected)
        if row is None:
            return False
        children = _navigation_rows(payload.get("children"), parent_run_id=selected)
        with self._lock:
            self._rows_by_run[selected] = row
            self._rows_by_parent[selected] = children
            for child in children:
                self._rows_by_run[str(child["run_id"])] = child
            child_ids = {str(child["run_id"]) for child in children}
            if self._selected_by_parent.get(selected, "") not in child_ids:
                self._selected_by_parent.pop(selected, None)
            runtime = self._runtime_for_locked(selected)
            first_goal = selected not in self._goal_published
            prior_final = self._final_responses.get(selected, "")
            final_response = str(payload.get("final_response") or "").strip()
            if final_response:
                self._final_responses[selected] = final_response
        goal = str(row.get("goal") or "").strip()
        if first_goal and goal:
            runtime.enqueue_prompt(f"agent-goal:{selected}", goal, queued=False)
            with self._lock:
                self._goal_published.add(selected)
        terminal = bool(payload.get("terminal"))
        status = str(row.get("status") or "").strip().upper()
        events = payload.get("transcript_events")
        if isinstance(events, list | tuple):
            runtime.publish_background_transcript_events(events)
        runtime.set_notice("")
        if terminal:
            runtime.settle_agent_transcript(selected, status=status)
        if final_response and final_response != prior_final:
            runtime.publish_background_response(final_response, thread_id=selected)
        main_activity = {
            "task_id": selected,
            "phase": _agent_activity_phase(status, terminal),
            "activity": str(row.get("activity") or "").strip(),
            "started_at": row.get("created_at", 0.0),
            "updated_at": row.get("updated_at", 0.0),
            "ended_at": row.get("ended_at", 0.0),
            **(
                {"context_usage": dict(raw_agent["context_usage"])}
                if isinstance(raw_agent.get("context_usage"), Mapping)
                else {}
            ),
        }
        runtime.update_background_activity(
            0 if terminal else 1,
            {
                "compact_count": max(0, _safe_int(row.get("compact_count"))),
                "main_activity": main_activity,
                "subagents": children,
                "task_progress": {
                    "items": payload.get("task_progress_items"),
                    "generation_id": payload.get("task_progress_generation_id"),
                    "plan_revision": payload.get("task_progress_plan_revision"),
                },
                "hidden_subagent_count": max(
                    0,
                    _safe_int(payload.get("hidden_child_count")),
                ),
                "projection_ok": True,
                "task_progress_projection_ok": isinstance(
                    payload.get("task_progress_items"), list | tuple
                ),
            },
        )
        with self._lock:
            self._event_cursors[selected] = max(
                self._event_cursors.get(selected, 0),
                _safe_int(payload.get("event_cursor")),
            )
        runtime.store.invalidate()
        return True

    # LLM: Internal active identity comes only from the view stack.
    # 函数用途: 在持锁状态下返回当前代理 run id，空串表示主代理。
    def _active_run_id_locked(self) -> str:
        return self._path[-1] if self._path else ""

    # LLM: Runtime allocation uses one session-local display namespace per run;
    # no Agent, worker, queue, or model backend is constructed.
    # 函数用途: 创建或取得一个子代理详情页的 typed 展示 runtime。
    def _runtime_for_locked(self, run_id: str) -> TuiRuntime:
        runtime = self._runtimes.get(run_id)
        if runtime is None:
            runtime = TuiRuntime(f"{self.root_runtime.session_id}:agent:{run_id}")
            self._runtimes[run_id] = runtime
        return runtime

    # LLM: Active runtime lookup mirrors the current stack and always retains
    # the root runtime as the empty-stack target.
    # 函数用途: 在持锁状态下返回当前页面的展示 runtime。
    def _active_runtime_locked(self) -> TuiRuntime:
        active = self._active_run_id_locked()
        return self._runtime_for_locked(active) if active else self.root_runtime


# LLM: Row normalization allows only bounded scalar display fields plus exact
# progress ids. Nested context usage remains outside row selection state.
# 函数用途: 清洗一个代理导航行，并校验可选的精确 run id。
def _navigation_row(
    value: object,
    *,
    expected_run_id: str = "",
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    run_id = str(value.get("run_id") or "").strip()
    if not run_id or (expected_run_id and run_id != expected_run_id):
        return None
    row = {
        str(key): value[key]
        for key in _ROW_SCALAR_FIELDS
        if key in value and not isinstance(value[key], dict | list | tuple | set)
    }
    row["run_id"] = run_id
    progress_ids = value.get("progress_item_ids")
    if isinstance(progress_ids, list | tuple):
        row["progress_item_ids"] = [
            str(item).strip()[:128]
            for item in progress_ids[:24]
            if str(item or "").strip()
        ]
    return row


# LLM: A row page must contain exact children of the supplied parent. Malformed
# or cross-parent rows are dropped rather than guessed into the visible tree.
# 函数用途: 清洗某一层的直属子代理列表。
def _navigation_rows(
    value: object,
    *,
    parent_run_id: str,
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    rows: list[dict[str, object]] = []
    for item in value[:64]:
        row = _navigation_row(item)
        if row is None:
            continue
        actual_parent = str(row.get("parent_run_id") or "").strip()
        if parent_run_id and actual_parent != parent_run_id:
            continue
        rows.append(row)
    return tuple(rows)


# LLM: Activity phase is a display mapping from canonical status/terminal facts;
# it cannot change the run state or decide success.
# 函数用途: 选择详情页主 Working 行的动画或终态样式。
def _agent_activity_phase(status: str, terminal: bool) -> str:
    if not terminal:
        return "running"
    if status == "DONE":
        return "completed"
    if status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return "failed"
    return "interrupted"


# LLM: Numeric coercion affects display counters/cursors only.
# 函数用途: 将不可信展示数字转换成非负整数。
def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["TuiAgentNavigationSnapshot", "TuiAgentNavigationState"]
