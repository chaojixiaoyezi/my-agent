"""Typed navigation state for entering and controlling delegated-agent views."""

# LLM: This module owns only TUI selection, view stack, per-view display runtimes,
# and event cursors. Canonical run status, messages, guidance, cancellation, and
# authorization remain on Gateway/domain services and are never inferred here.
# 结束页正文必须保留原 thread/message ID，不能以正文哈希或 legacy 文案创建身份。
# 模块用途: 支持空输入时方向键选择 Goal/子代理、Enter 查看、Ctrl+G 返回，并缓存每个代理的独立展示页面。

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ...agent.conversation.background_transcript import BACKGROUND_TRANSCRIPT_SCHEMA
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
_AGENT_STARTUP_NOTICE_KIND = "agent_startup"
_ROW_SCALAR_FIELDS = frozenset(
    {
        "run_id",
        "root_task_id",
        "parent_run_id",
        "depth",
        "name",
        "role",
        "status",
        "lifecycle_phase",
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
_GOAL_ROW_SCALAR_FIELDS = frozenset(
    {
        "goal_id",
        "revision",
        "name",
        "objective",
        "status",
        "tokens_used",
        "token_budget",
        "time_used_seconds",
        "duration_seconds",
        "created_at",
        "updated_at",
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
    expanded_goal_id: str = ""
    depth: int = 0
    terminal: bool = False


# LLM: This immutable projection is the only normalized input accepted by the child
# view updater. It contains display facts, not control authority or executable objects.
# 类用途: 保存一次子代理详情响应中已经校验过的行、事件和终态展示字段。
@dataclass(frozen=True)
class _AgentViewProjection:
    run_id: str
    payload: Mapping[str, object]
    raw_agent: Mapping[str, object]
    row: dict[str, object]
    children: tuple[dict[str, object], ...]
    events: object
    typed_final_request_ids: frozenset[str]
    final_response: str
    final_response_request_id: str
    final_response_key: str
    terminal: bool
    status: str
    lifecycle_phase: str


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
        self._goal_rows: dict[str, dict[str, object]] = {}
        self._child_goal_rows: dict[str, dict[str, dict[str, object]]] = {}
        self._selected_by_parent: dict[str, str] = {}
        self._expanded_goal_id = ""
        self._path: list[str] = []
        self._event_cursors: dict[str, int] = {}
        self._goal_published: set[str] = set()
        self._final_response_keys: dict[str, str] = {}
        self._typed_final_response_request_ids: set[str] = set()
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
            if selected and selected not in self._selection_ids_locked(parent):
                self._selected_by_parent.pop(parent, None)
            runtime = self._runtimes.get(parent) if parent else self.root_runtime
        if runtime is not None:
            runtime.store.invalidate()
        return True

    # LLM: Goal rows are exact read-only projections, scoped to the root or the
    # current child view. Goal IDs cannot stand in for agent run IDs or authorize control.
    # 函数用途: 刷新当前主子代理视角底部 Goal 行，让方向键和编辑器指向本代理的准确目标。
    def update_goal_rows(self, value: object, *, parent_run_id: str = "") -> bool:
        rows = _goal_navigation_rows(value)
        next_rows = {
            _goal_navigation_id(str(row["goal_id"])): row for row in rows
        }
        with self._lock:
            if parent_run_id:
                if self._child_goal_rows.get(parent_run_id) == next_rows:
                    return False
                self._child_goal_rows[parent_run_id] = next_rows
                self._runtime_for_locked(parent_run_id).store.invalidate()
                return True
            if self._goal_rows == next_rows:
                return False
            self._goal_rows = next_rows
            selected = self._selected_by_parent.get("", "")
            if selected and selected not in self._selection_ids_locked(""):
                self._selected_by_parent.pop("", None)
            if self._expanded_goal_id and self._expanded_goal_id not in next_rows:
                self._expanded_goal_id = ""
            runtime = self.root_runtime
        runtime.store.invalidate()
        return True

    # LLM: Return a copy of the exact current-page selection; it is display data, never control authorization.
    # 函数用途: 编辑器取得被方向键选中的目标，Gateway 保存时仍会重新校验归属和版本。
    def selected_goal(self) -> dict[str, object] | None:
        with self._lock:
            parent = self._active_run_id_locked()
            rows = self._child_goal_rows.get(parent, {}) if parent else self._goal_rows
            row = rows.get(self._selected_by_parent.get(parent, ""))
            return dict(row) if row is not None else None

    # LLM: Direction movement is active only when the input controller explicitly
    # calls it. It never consumes history or changes the current view itself.
    # 函数用途: 在当前层的 Goal/子代理行之间移动选择；首次向下选第一行，首次向上选最后一行。
    def move_selection(self, delta: int) -> bool:
        step = int(delta or 0)
        if not step:
            return False
        with self._lock:
            parent = self._active_run_id_locked()
            ids = self._selection_ids_locked(parent)
            if not ids:
                return False
            selected = self._selected_by_parent.get(parent, "")
            if selected not in ids:
                index = 0 if step > 0 else len(ids) - 1
            else:
                index = max(0, min(len(ids) - 1, ids.index(selected) + step))
            self._selected_by_parent[parent] = ids[index]
            if not parent and self._expanded_goal_id != ids[index]:
                self._expanded_goal_id = ""
            runtime = self._active_runtime_locked()
        runtime.store.invalidate()
        return True

    # LLM: Enter follows an exact typed selection from the authenticated Goal
    # or child row set. Goal toggles read-only detail in root; a child opens its
    # display runtime. Neither action trusts an id supplied from input text.
    # 函数用途: 展开当前 Goal 或进入选中的子代理详情页；子代理快照到达前显示载入提示。
    def enter_selected(self) -> bool:
        with self._lock:
            parent = self._active_run_id_locked()
            selected = self._selected_by_parent.get(parent, "")
            if not selected or selected not in self._selection_ids_locked(parent):
                return False
            goals = self._child_goal_rows.get(parent, {}) if parent else self._goal_rows
            if selected in goals:
                self._expanded_goal_id = "" if self._expanded_goal_id == selected else selected
                runtime = self._active_runtime_locked()
                callback = None
                has_snapshot = True
                lifecycle_phase = ""
            else:
                self._expanded_goal_id = ""
                self._path.append(selected)
                runtime = self._runtime_for_locked(selected)
                callback = self._view_change
                has_snapshot = selected in self._goal_published
                lifecycle_phase = str(
                    self._rows_by_run.get(selected, {}).get("lifecycle_phase") or ""
                )
        if not has_snapshot:
            runtime.set_notice(
                _agent_startup_notice(lifecycle_phase),
                duration_seconds=2.5,
                notice_kind=_AGENT_STARTUP_NOTICE_KIND,
            )
        if callback is not None:
            callback(runtime)
        runtime.store.invalidate()
        return True

    # LLM: Back pops exactly one visual parent. It does not cancel, pause, resume,
    # or mutate either agent, which is why Esc is deliberately not routed here.
    # Before showing the parent, its cached roster is reconciled only with newer
    # typed rows already observed on the descendant page.
    # 函数用途: 用 Ctrl+G、Alt+左箭头或 `/back` 返回上一层，并先修正已知的过期子代理展示状态。
    def back(self) -> bool:
        reconcile_rows: tuple[dict[str, object], ...] | None = None
        with self._lock:
            if self._expanded_goal_id:
                self._expanded_goal_id = ""
                runtime = self._active_runtime_locked()
                callback = None
            elif not self._path:
                return False
            else:
                self._path.pop()
                parent = self._active_run_id_locked()
                reconcile_rows = _reconcile_parent_rows(
                    self._rows_by_parent.get(parent, ()),
                    self._rows_by_run,
                )
                self._rows_by_parent[parent] = reconcile_rows
                runtime = self._active_runtime_locked()
                callback = self._view_change
        if reconcile_rows is not None:
            reconciler = getattr(runtime, "reconcile_background_subagents", None)
            if callable(reconciler):
                reconciler(reconcile_rows)
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
            if not parent:
                self._expanded_goal_id = ""
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
                expanded_goal_id=self._expanded_goal_id,
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
    # 函数用途: 把子代理消息快照应用到对应页面；正式回复用 canonical thread/message ID，发布后才确认。
    def apply_agent_view(self, run_id: str, payload: object) -> bool:
        view = _normalize_agent_view_projection(run_id, payload)
        if view is None:
            return False
        runtime, first_goal, prior_final_key, typed_final_already_visible = (
            self._register_agent_view(view)
        )
        if view.payload.get("goal_projection_ok") is not False:
            self.update_goal_rows(view.payload.get("goals") or [], parent_run_id=view.run_id)
        goal = str(view.row.get("goal") or "").strip()
        if first_goal and goal:
            runtime.enqueue_prompt(f"agent-goal:{view.run_id}", goal, queued=False)
            with self._lock:
                self._goal_published.add(view.run_id)
        with self._lock:
            goal_published = view.run_id in self._goal_published
        if isinstance(view.events, list | tuple):
            runtime.publish_background_transcript_events(view.events)
        if view.terminal or goal_published or bool(view.events) or view.final_response:
            runtime.clear_notice(expected_kind=_AGENT_STARTUP_NOTICE_KIND)
        else:
            # 轮询会续上这个短 notice；一旦 goal/首事件到达立即清除。这样既不新增
            # 持久状态，也不会因为首个不完整快照把详情页变成无解释白屏。
            runtime.set_notice(
                _agent_startup_notice(view.lifecycle_phase),
                duration_seconds=2.5,
                notice_kind=_AGENT_STARTUP_NOTICE_KIND,
            )
        if view.terminal:
            runtime.settle_agent_transcript(view.run_id, status=view.status)
        if (
            view.final_response
            and view.final_response_key != prior_final_key
            and not typed_final_already_visible
        ):
            runtime.publish_background_response(
                view.final_response,
                thread_id=str(view.payload.get("thread_id") or ""),
                message_id=view.final_response_key,
            )
        runtime.update_background_activity(
            0 if view.terminal else 1,
            _agent_view_background_activity(view),
        )
        with self._lock:
            if view.final_response_key:
                self._final_response_keys[view.run_id] = view.final_response_key
            self._event_cursors[view.run_id] = max(
                self._event_cursors.get(view.run_id, 0),
                _safe_int(view.payload.get("event_cursor")),
            )
        runtime.store.invalidate()
        return True

    # LLM: Registry mutation is kept under one lock so child rows, selection,
    # typed final identity, and display runtime become visible as one UI projection.
    # Canonical final acknowledgement happens only after publication, not during registration.
    # 函数用途: 原子登记一次已校验的子代理详情，并返回渲染阶段所需的去重状态。
    def _register_agent_view(
        self,
        view: _AgentViewProjection,
    ) -> tuple[TuiRuntime, bool, str, bool]:
        with self._lock:
            self._rows_by_run[view.run_id] = view.row
            self._rows_by_parent[view.run_id] = view.children
            for child in view.children:
                self._rows_by_run[str(child["run_id"])] = child
            child_ids = set(self._selection_ids_locked(view.run_id))
            if self._selected_by_parent.get(view.run_id, "") not in child_ids:
                self._selected_by_parent.pop(view.run_id, None)
            runtime = self._runtime_for_locked(view.run_id)
            first_goal = view.run_id not in self._goal_published
            prior_final_key = self._final_response_keys.get(view.run_id, "")
            self._typed_final_response_request_ids.update(view.typed_final_request_ids)
            typed_final_already_visible = bool(
                view.final_response_request_id
                and view.final_response_request_id
                in self._typed_final_response_request_ids
            )
        return runtime, first_goal, prior_final_key, typed_final_already_visible

    # LLM: Internal active identity comes only from the view stack.
    # 函数用途: 在持锁状态下返回当前代理 run id，空串表示主代理。
    def _active_run_id_locked(self) -> str:
        return self._path[-1] if self._path else ""

    # LLM: Root selection is Goal rows followed by direct children; nested
    # pages contain children only. This is a visual ordering contract and never
    # changes either lifecycle.
    # 函数用途: 返回当前页面方向键可以选择的准确行 ID。
    def _selection_ids_locked(self, parent_run_id: str) -> list[str]:
        parent = str(parent_run_id or "").strip()
        ids = list(self._goal_rows) if not parent else list(self._child_goal_rows.get(parent, {}))
        ids.extend(
            str(row["run_id"])
            for row in self._rows_by_parent.get(parent, ())
            if str(row.get("run_id") or "").strip()
        )
        return ids

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


# LLM: Descendant detail polling may hold a newer typed row than the parent's
# cached roster. Merge only rows whose structured updated_at is at least as new;
# no status is inferred or made monotonic.
# 函数用途: 返回上一层时把刚查看代理的最新状态合回父列表，避免旧缓存短暂把已停止代理画成运行中。
def _reconcile_parent_rows(
    cached: tuple[dict[str, object], ...],
    rows_by_run: Mapping[str, dict[str, object]],
) -> tuple[dict[str, object], ...]:
    reconciled: list[dict[str, object]] = []
    for row in cached:
        run_id = str(row.get("run_id") or "").strip()
        latest = rows_by_run.get(run_id)
        if latest is None or _row_updated_at(latest) < _row_updated_at(row):
            reconciled.append(row)
        else:
            reconciled.append(dict(latest))
    return tuple(reconciled)


# LLM: Gateway detail payloads cross an untrusted presentation boundary. Normalize
# exact run identity and bounded row/event facts once before mutating navigation state.
# A final without its canonical thread/message id is not guessed from prose or a legacy alias.
# 函数用途: 校验并整理一份子代理详情响应，非法或串代理的数据直接拒绝。
def _normalize_agent_view_projection(
    run_id: str,
    payload: object,
) -> _AgentViewProjection | None:
    selected = str(run_id or "").strip()
    if not selected or not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return None
    raw_agent = payload.get("agent")
    if not isinstance(raw_agent, Mapping):
        return None
    row = _navigation_row(raw_agent, expected_run_id=selected)
    if row is None:
        return None
    children = _navigation_rows(payload.get("children"), parent_run_id=selected)
    events = payload.get("transcript_events")
    final_response = str(payload.get("final_response") or "").strip()
    final_response_request_id = _final_response_request_id(
        payload.get("final_response_request_id"),
        selected,
    )
    final_response_key = str(payload.get("final_response_message_id") or "").strip()
    if not final_response_key or not str(payload.get("thread_id") or "").strip():
        final_response = ""
    return _AgentViewProjection(
        run_id=selected,
        payload=payload,
        raw_agent=raw_agent,
        row=row,
        children=children,
        events=events,
        typed_final_request_ids=frozenset(
            _typed_final_response_request_ids(events, selected)
        ),
        final_response=final_response,
        final_response_request_id=final_response_request_id,
        final_response_key=final_response_key,
        terminal=bool(payload.get("terminal")),
        status=str(row.get("status") or "").strip().upper(),
        lifecycle_phase=str(row.get("lifecycle_phase") or "").strip().lower(),
    )


# LLM: Activity rendering derives only from the normalized child projection. Keep
# this payload compatible with TuiRuntime and never infer lifecycle from prose.
# 函数用途: 生成子代理详情页底部 Working、上下文、Todo 和下级代理的展示数据。
def _agent_view_background_activity(view: _AgentViewProjection) -> dict[str, object]:
    row = view.row
    payload = view.payload
    main_activity = {
        "task_id": view.run_id,
        "phase": _agent_activity_phase(
            view.status,
            view.terminal,
            lifecycle_phase=view.lifecycle_phase,
        ),
        "activity": str(row.get("activity") or "").strip(),
        "started_at": row.get("created_at", 0.0),
        "updated_at": row.get("updated_at", 0.0),
        "ended_at": row.get("ended_at", 0.0),
        **(
            {"context_usage": dict(view.raw_agent["context_usage"])}
            if isinstance(view.raw_agent.get("context_usage"), Mapping)
            else {}
        ),
    }
    return {
        "compact_count": max(0, _safe_int(row.get("compact_count"))),
        "model_metrics": view.raw_agent.get("model_metrics"),
        "main_activity": main_activity,
        "subagents": view.children,
        "goals": payload.get("goals"),
        "goal_projection_ok": payload.get("goal_projection_ok") is not False,
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
    }


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


# LLM: Goal navigation ids are an explicit namespace distinct from run ids, so
# an Enter action can never accidentally call the child-agent endpoint.
# 函数用途: 为底部 Goal 行生成稳定且不会与子代理冲突的选择 ID。
def _goal_navigation_id(goal_id: str) -> str:
    return f"goal:{str(goal_id or '').strip()}"


# LLM: Goal row normalization accepts only bounded public scalars supplied by
# the authenticated conversation activity projection.
# 函数用途: 清洗可供方向键选择和详情展示的 Goal 列表。
def _goal_navigation_rows(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    rows: list[dict[str, object]] = []
    for item in value[:16]:
        if not isinstance(item, Mapping):
            continue
        goal_id = str(item.get("goal_id") or "").strip()
        if not goal_id:
            continue
        row = {
            str(key): item[key]
            for key in _GOAL_ROW_SCALAR_FIELDS
            if key in item and not isinstance(item[key], dict | list | tuple | set)
        }
        row["goal_id"] = goal_id
        rows.append(row)
    return tuple(rows)


# LLM: A typed non-process assistant completion is the canonical visible final
# for one request. Record only exact bg-agent identities from the frozen public
# transcript schema so a later history fallback cannot append the same reply.
# 函数用途: 从本次子代理事件里提取已经显示过的正式最终回复回合标识。
def _typed_final_response_request_ids(value: object, run_id: str) -> set[str]:
    if not isinstance(value, list | tuple):
        return set()
    prefix = f"bg-agent:{str(run_id or '').strip()}:"
    request_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        request_id = str(item.get("request_id") or "").strip()
        block_id = str(item.get("block_id") or "").strip()
        payload = item.get("payload")
        if (
            item.get("schema") != BACKGROUND_TRANSCRIPT_SCHEMA
            or item.get("kind") != "assistant_completed"
            or item.get("phase") != "completed"
            or not request_id.startswith(prefix)
            or not block_id.startswith(f"{request_id}:")
            or not isinstance(payload, Mapping)
            or payload.get("process") is True
            or not str(payload.get("text") or "").strip()
        ):
            continue
        request_ids.add(request_id)
    return request_ids


# LLM: The final-response fallback may join only an exact request identity in
# the current child namespace. Invalid values remain empty; the independent canonical
# message ID still owns history identity, never a key derived from response prose.
# 函数用途: 校验 Gateway 返回的最终回复回合标识确实属于当前子代理。
def _final_response_request_id(value: object, run_id: str) -> str:
    request_id = str(value or "").strip()
    prefix = f"bg-agent:{str(run_id or '').strip()}:"
    if not request_id.startswith(prefix) or len(request_id) > 512:
        return ""
    return request_id


# LLM: Activity phase is a display mapping from canonical status/terminal facts;
# it cannot change the run state or decide success.
# 函数用途: 选择详情页主 Working 行的动画或终态样式。
def _agent_activity_phase(
    status: str,
    terminal: bool,
    *,
    lifecycle_phase: str = "",
) -> str:
    if not terminal:
        phase = str(lifecycle_phase or "").strip().lower()
        return phase if phase in {"queued", "starting", "waiting_first_event"} else "running"
    if status == "DONE":
        return "completed"
    if status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return "failed"
    return "interrupted"


# LLM: Startup copy is selected only from the authenticated lifecycle_phase scalar. It is a
# transient TUI notice and must never infer status from task text, timestamps, or spinner frames.
# 函数用途: 在子代理详情尚无 prompt/事件时，用大白话持续解释当前启动阶段。
def _agent_startup_notice(lifecycle_phase: str) -> str:
    return {
        "queued": "子代理已创建，正在排队…",
        "starting": "子代理正在启动执行器…",
        "waiting_first_event": "子代理已开始，正在等待模型首个响应…",
        "running": "子代理正在运行，正在载入首段详情…",
    }.get(str(lifecycle_phase or "").strip().lower(), "正在载入子代理详情…")


# LLM: Numeric coercion affects display counters/cursors only.
# 函数用途: 将不可信展示数字转换成非负整数。
def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Row recency arbitration is display-only and uses the canonical scalar
# already supplied by the backend; invalid timestamps are simply oldest.
# 函数用途: 读取子代理展示行的更新时间，用来避免返回父页面时拿旧快照覆盖新状态。
def _row_updated_at(value: Mapping[str, object]) -> float:
    try:
        return max(0.0, float(value.get("updated_at") or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["TuiAgentNavigationSnapshot", "TuiAgentNavigationState"]
