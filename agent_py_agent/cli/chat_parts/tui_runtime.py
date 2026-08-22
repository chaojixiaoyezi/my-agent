# LLM: 本模块是 chat worker/Gateway structured rows 到 TuiEvent 的唯一 adapter；它不渲染、不执行工具，也不把 legacy 文案当状态。
# 模块用途: 为 session、输入队列、回合、流式助手、思考和工具生命周期分配稳定 block id 并发布有序 typed events。

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ...agent.contracts.tool_approval import (
    ToolApprovalDecision,
    ToolApprovalRequest,
)
from .tui_events import JournalAppendResult, TuiEvent, TuiEventSequencer
from .tui_view_model import TuiStateStore


# LLM: TuiTurnSummary 是 finalize 的结构化输入；error/status/token 不能从格式化 timing 文案反解析。
# 类用途: 汇总一个 worker 回合的最终响应和统计。
@dataclass(frozen=True)
class TuiTurnSummary:
    response_text: str = ""
    ok: bool = True
    interrupted: bool = False
    error: str = ""
    context_tokens: int = 0
    output_tokens: int = 0
    tool_rounds: int = 0


# LLM: pending 对象只在 adapter 内桥接审批线程；request 是授权身份，Event/decision 仅负责唤醒同一等待者。
# 类用途: 保存当前尚未答复的工具审批及其线程同步状态。
@dataclass
class _PendingTuiPermission:
    request: ToolApprovalRequest
    ready: threading.Event
    decision: ToolApprovalDecision | None = None
    feedback_by_option: dict[str, str] = field(default_factory=dict)
    feedback_mode: bool = False


# LLM: 该控制器独占一个 turn 的审批等待者、选择草稿和 Gateway sink；adapter 只保留事件流生命周期，避免两套审批状态。
# 类用途: 管理本地/Gateway 工具确认框的打开、编辑、决定、取消及跨线程唤醒。
class _TuiPermissionController:
    # LLM: owner 是唯一 TuiTurnEventAdapter；控制器复用 owner 锁和 publish 入口，不创建第二 sequencer/store。
    # 函数用途: 为一个活动回合初始化审批状态。
    def __init__(self, owner: TuiTurnEventAdapter) -> None:
        self._owner = owner
        self._pending: _PendingTuiPermission | None = None
        self._sink: Callable[[ToolApprovalRequest, ToolApprovalDecision], None] | None = None
        self._resolved_ids: set[str] = set()

    # LLM: Gateway sink 只负责原子写决定文件，request/decision 都已由 shared approval contract 校验；本地模式保持 None。
    # 函数用途: 为当前 Gateway 回合安装审批答复写回函数。
    def configure_gateway_sink(
        self,
        sink: Callable[[ToolApprovalRequest, ToolApprovalDecision], None],
    ) -> None:
        if not callable(sink):
            raise TypeError("gateway permission sink must be callable")
        with self._owner._lock:
            self._sink = sink

    # LLM: 清理仅移除跨进程 sink；尚未收口的 pending request 会在 finalize 中按 cancelled 关闭，不能静默丢失。
    # 函数用途: 在 Gateway 轮询结束后解除审批文件写回函数。
    def clear_gateway_sink(self) -> None:
        with self._owner._lock:
            self._sink = None

    # LLM: 本地 ToolExecutor 在该方法内等待 UI typed decision；取消令牌可打断等待，批准仍只由精确 request binding 生成。
    # 函数用途: 发布本地工具审批面板并阻塞到用户选择或取消。
    def request_permission(
        self,
        request_value: Mapping[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        request = ToolApprovalRequest.from_mapping(request_value)
        pending = self.open(request)
        while not pending.ready.wait(0.05):
            if _cancellation_requested(cancellation_token):
                self.resolve(request.permission_id, "cancelled")
        decision = pending.decision or ToolApprovalDecision(
            request.permission_id,
            "unavailable",
        )
        return decision.to_dict()

    # LLM: selection 事件必须命中当前 pending id 并在 options 范围内环绕；不存在面板时不产生 journal 噪声。
    # 函数用途: 移动当前审批选项并请求重绘。
    def move_selection(self, delta: int) -> bool:
        with self._owner._lock:
            pending = self._pending
            if pending is None or not pending.request.options:
                return False
            overlay = self._owner.runtime.store.snapshot().permission
            if overlay is None or overlay.permission_id != pending.request.permission_id:
                return False
            index = (overlay.selected_index + int(delta or 0)) % len(
                pending.request.options
            )
            option = pending.request.options[index]
            option_id = str(option.get("id") or "")
            pending.feedback_mode = False
            self._publish(
                "permission_selection_changed",
                {
                    "permission_id": pending.request.permission_id,
                    "selected_index": index,
                    "feedback_mode": False,
                    "feedback": pending.feedback_by_option.get(option_id, ""),
                    "feedback_placeholder": str(option.get("feedback_placeholder") or ""),
                },
            )
            return True

    # LLM: 只有声明 feedback_type 的当前选项支持 Tab；模式变化保留该选项自己的草稿但不修改审批决定。
    # 函数用途: 切换当前 Yes/No 选项的补充说明输入模式。
    def toggle_feedback(self) -> bool:
        with self._owner._lock:
            pending = self._pending
            overlay = self._owner.runtime.store.snapshot().permission
            if (
                pending is None
                or overlay is None
                or overlay.permission_id != pending.request.permission_id
            ):
                return False
            option = pending.request.options[overlay.selected_index]
            if not str(option.get("feedback_type") or ""):
                return False
            option_id = str(option.get("id") or "")
            pending.feedback_mode = not pending.feedback_mode
            self._publish(
                "permission_feedback_toggled",
                {
                    "permission_id": pending.request.permission_id,
                    "selected_index": overlay.selected_index,
                    "feedback_mode": pending.feedback_mode,
                    "feedback": pending.feedback_by_option.get(option_id, ""),
                    "feedback_placeholder": str(option.get("feedback_placeholder") or ""),
                },
            )
            return True

    # LLM: 文本草稿按 option id 隔离，且只在该 permission 的 feedback mode 中接受；文案本身没有控制权。
    # 函数用途: 更新当前权限选择的补充说明文本。
    def update_feedback(self, permission_id: str, text: str) -> bool:
        normalized_id = str(permission_id or "").strip()
        with self._owner._lock:
            pending = self._pending
            overlay = self._owner.runtime.store.snapshot().permission
            if (
                pending is None
                or overlay is None
                or normalized_id != pending.request.permission_id
                or not pending.feedback_mode
            ):
                return False
            option = pending.request.options[overlay.selected_index]
            option_id = str(option.get("id") or "")
            pending.feedback_by_option[option_id] = str(text or "")
            self._publish(
                "permission_feedback_changed",
                {
                    "permission_id": pending.request.permission_id,
                    "selected_index": overlay.selected_index,
                    "feedback": str(text or ""),
                },
            )
            return True

    # LLM: decision 必须属于 request 暴露的 option；外部 sink 成功后才关闭 overlay，本地等待者通过同一 Event 被唤醒。
    # 函数用途: 处理 Enter/Esc/Ctrl-C 产生的审批选择。
    def resolve(
        self,
        permission_id: str,
        decision_value: str,
        *,
        feedback: str = "",
    ) -> bool:
        normalized_id = str(permission_id or "").strip()
        normalized_decision = str(decision_value or "").strip().lower()
        with self._owner._lock:
            pending = self._pending
            if pending is None or pending.request.permission_id != normalized_id:
                return False
            allowed = {str(item.get("decision") or "") for item in pending.request.options}
            if normalized_decision not in allowed and normalized_decision != "cancelled":
                return False
            overlay = self._owner.runtime.store.snapshot().permission
            selected_option = (
                pending.request.options[overlay.selected_index]
                if overlay is not None and overlay.permission_id == normalized_id
                else {}
            )
            option_id = str(selected_option.get("id") or "")
            resolved_feedback = str(feedback or pending.feedback_by_option.get(option_id, ""))
            decision = ToolApprovalDecision(
                normalized_id,
                normalized_decision,
                resolved_feedback,
            )
            sink = self._sink
        if sink is not None:
            try:
                sink(pending.request, decision)
            except (OSError, TypeError, ValueError):
                return False
        with self._owner._lock:
            if self._pending is not pending:
                return False
            pending.decision = decision
            pending.ready.set()
            self._pending = None
            self._resolved_ids.add(normalized_id)
            self._publish(
                "permission_resolved",
                decision.to_dict(),
                phase="completed",
                request=pending.request,
            )
            return True

    # LLM: request 打开顺序必须在 tool_started 之后且一次仅一个；重复同 id 幂等，冲突请求显式失败而不覆盖等待者。
    # 函数用途: 建立 pending 对象并发布 permission_requested UI 事件。
    def open(self, request: ToolApprovalRequest) -> _PendingTuiPermission:
        with self._owner._lock:
            current = self._pending
            if current is not None:
                if current.request.permission_id == request.permission_id:
                    return current
                raise RuntimeError("another TUI permission request is already pending")
            pending = _PendingTuiPermission(request, threading.Event())
            self._pending = pending
            self._owner._complete_active_assistant(process=True)
            self._publish("permission_requested", request.to_dict(), phase="waiting_permission")
            return pending

    # LLM: Gateway terminal resolution 可能在本地 UI 已写回后重放；同 id 重放只消费，不制造 reducer mismatch。
    # 函数用途: 接收 Gateway writer 发布的最终审批决定。
    def accept_gateway_resolution(self, payload: Mapping[str, object]) -> bool:
        decision = ToolApprovalDecision.from_mapping(payload)
        with self._owner._lock:
            if decision.permission_id in self._resolved_ids:
                return True
            pending = self._pending
            if pending is None or pending.request.permission_id != decision.permission_id:
                return False
            pending.decision = decision
            pending.ready.set()
            self._pending = None
            self._resolved_ids.add(decision.permission_id)
            self._publish(
                "permission_resolved",
                decision.to_dict(),
                phase="completed",
                request=pending.request,
            )
            return True

    # LLM: turn 收口时仍在等待的审批必须成为 cancelled 并唤醒本地线程；不得留下悬挂 Event 或可迟到批准的 sink。
    # 函数用途: 取消并关闭当前 pending 审批。
    def cancel_pending(self) -> None:
        pending = self._pending
        if pending is None:
            return
        decision = ToolApprovalDecision(pending.request.permission_id, "cancelled")
        pending.decision = decision
        pending.ready.set()
        self._pending = None
        self._resolved_ids.add(decision.permission_id)
        self._publish(
            "permission_resolved",
            decision.to_dict(),
            phase="completed",
            request=pending.request,
        )

    # LLM: controller 只能经 owner runtime 发布同一 permission 的 typed event；block id 由 request round/index 事实生成。
    # 函数用途: 统一发布审批覆盖层事件，避免各交互分支重复拼装身份字段。
    def _publish(
        self,
        kind: str,
        payload: dict[str, object],
        *,
        phase: str = "updated",
        request: ToolApprovalRequest | None = None,
    ) -> None:
        pending = self._pending
        effective_request = request or (pending.request if pending is not None else None)
        permission_id = str(payload.get("permission_id") or "")
        if effective_request is None or (
            permission_id and permission_id != effective_request.permission_id
        ):
            return
        self._owner.runtime._publish(
            kind,
            phase,
            _tool_block_id(self._owner.request_id, effective_request.to_dict()),
            payload,
            request_id=self._owner.request_id,
        )


# LLM: This controller is runtime-owned and only factors the removable
# background display state out of TuiRuntime; it shares the runtime lock,
# sequencer, and store and must never become a second activity authority.
# 类用途: 保存一个 TUI 会话的后台 Working 计数、直属子代理快照和起始时间，并发布对应 typed 事件。
class _TuiBackgroundActivityController:
    # LLM: The owner remains the only event publisher and lock owner.
    # 函数用途: 绑定唯一 TuiRuntime，并初始化空闲显示状态。
    def __init__(self, owner: TuiRuntime) -> None:
        self._owner = owner
        self._count = 0
        self._started_at = 0.0
        self._main_activity: dict[str, object] = {}
        self._subagents: tuple[dict[str, object], ...] = ()
        self._hidden_subagent_count = 0

    # LLM: Count and direct-child rows change the same display projection. A
    # failed child projection preserves the last valid rows; zero removes the
    # block without transcript output.
    # 函数用途: 根据 canonical 主任务和直属子代理快照开始、更新或收起底部 Working 区。
    def update(
        self,
        active_task_count: int,
        *,
        main_activity: object | None = None,
        subagents: object | None = None,
        hidden_subagent_count: int = 0,
        projection_ok: bool = True,
    ) -> bool:
        count = max(0, int(active_task_count or 0))
        owner = self._owner
        block_id = f"background-activity:{owner.session_id}"
        with owner._lock:
            if count <= 0:
                next_main_activity: dict[str, object] = {}
                next_subagents: tuple[dict[str, object], ...] = ()
                next_hidden_count = 0
            else:
                next_main_activity = (
                    _normalize_main_activity(main_activity)
                    if main_activity is not None
                    else self._main_activity
                )
                if projection_ok and subagents is not None:
                    next_subagents = _normalize_subagent_activity_rows(subagents)
                    next_hidden_count = max(0, int(hidden_subagent_count or 0))
                else:
                    next_subagents = self._subagents
                    next_hidden_count = self._hidden_subagent_count
            if (
                count == self._count
                and next_main_activity == self._main_activity
                and next_subagents == self._subagents
                and next_hidden_count == self._hidden_subagent_count
            ):
                return False
            if self._count <= 0 and count > 0:
                self._started_at = time.time()
                kind, phase = "background_activity_started", "started"
            elif count > 0:
                kind, phase = "background_activity_updated", "updated"
            else:
                kind, phase = "background_activity_completed", "completed"
            self._count = count
            self._main_activity = next_main_activity
            self._subagents = next_subagents
            self._hidden_subagent_count = next_hidden_count
            owner._publish(
                kind,
                phase,
                block_id,
                {
                    "active_task_count": count,
                    "started_at": self._started_at,
                    "main_activity": dict(next_main_activity),
                    "subagents": [dict(row) for row in next_subagents],
                    "hidden_subagent_count": next_hidden_count,
                },
                request_id=f"background:{owner.session_id}",
            )
            if count <= 0:
                self._started_at = 0.0
        return True


_SUBAGENT_ACTIVITY_ROW_LIMIT = 64
_MAIN_ACTIVITY_FIELDS = frozenset(
    {"task_id", "phase", "activity", "started_at", "updated_at"}
)
_SUBAGENT_ACTIVITY_FIELDS = frozenset(
    {
        "run_id",
        "root_task_id",
        "parent_run_id",
        "depth",
        "name",
        "role",
        "status",
        "activity",
        "current_tool",
        "attempts",
        "token_count",
        "compact_count",
        "progress_item_ids",
        "created_at",
        "updated_at",
        "heartbeat_at",
        "ended_at",
    }
)


# LLM: Main activity is an authenticated scalar-only Gateway projection. Nested
# values and unknown keys are discarded before entering the reducer metadata.
# 函数用途: 清洗后台主代理的实时活动行，避免路径、输出或其它内部字段进入 TUI。
def _normalize_main_activity(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): value[key]
        for key in _MAIN_ACTIVITY_FIELDS
        if key in value and not isinstance(value[key], dict | list | tuple | set)
    }


# LLM: This is the client-side metadata whitelist for the authenticated activity
# snapshot. Only bounded progress ids may remain a list; all other nested values,
# goals, paths, and tool output are discarded before TuiBlock metadata.
# 函数用途: 清洗 Gateway 的直属子代理展示行，并限制 Todo 关联 ID 的数量与长度。
def _normalize_subagent_activity_rows(
    value: object,
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    rows: list[dict[str, object]] = []
    for item in value[:_SUBAGENT_ACTIVITY_ROW_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        row = {
            str(key): item[key]
            for key in _SUBAGENT_ACTIVITY_FIELDS
            if key in item and not isinstance(item[key], dict | list | tuple | set)
        }
        progress_ids = item.get("progress_item_ids")
        if isinstance(progress_ids, list | tuple):
            row["progress_item_ids"] = list(
                dict.fromkeys(
                    str(value).strip()[:128]
                    for value in progress_ids[:24]
                    if str(value or "").strip()
                )
            )
        rows.append(row)
    return tuple(rows)


# LLM: This narrow mixin keeps the public display adapter out of the central
# runtime class size budget. It only delegates to the runtime-owned controller
# and must not gain independent state or event sequencing.
# 类用途: 为 TuiRuntime 提供后台主任务和直属子代理快照的唯一公开更新入口。
class _TuiBackgroundActivityRuntimeMixin:
    # LLM: This display projection accepts only the canonical conversation-agent
    # snapshot. It never starts, stops, retries, or accepts work; equal snapshots
    # are idempotent and zero removes the active block without history.
    # 函数用途: 在输入框附近持续显示真实主任务和直属子代理，任务清零时原位收起。
    def update_background_activity(
        self,
        active_task_count: int,
        *,
        main_activity: object | None = None,
        subagents: object | None = None,
        hidden_subagent_count: int = 0,
        projection_ok: bool = True,
    ) -> bool:
        return self._background_activity.update(
            active_task_count,
            main_activity=main_activity,
            subagents=subagents,
            hidden_subagent_count=hidden_subagent_count,
            projection_ok=projection_ok,
        )

    # LLM: Every legacy background notice receives a fresh block id so repeated
    # updates cannot collapse in the reducer. The method is display-only.
    # 函数用途: 把后台进度或旧版通知显示为独立的灰色系统消息。
    def publish_background_notice(self, summary: str, *, thread_id: str = "") -> None:
        text = str(summary or "").strip()
        if not text:
            return
        request_id = f"bg-notice:{thread_id or self.session_id}"
        with self._lock:
            self._background_notice_index += 1
            block_id = f"{request_id}:{self._background_notice_index}"
            self._publish(
                "system_message",
                "completed",
                block_id,
                {"text": text, "severity": "info"},
                request_id=request_id,
            )

    # LLM: A committed background owner reply must enter transcript as the same
    # assistant role as a foreground final; this method does not deliver or rerun it.
    # 函数用途: 把 Gateway 已提交的后台主代理最终回复显示为普通助手消息，而不是灰色系统告警。
    def publish_background_response(self, content: str, *, thread_id: str = "") -> None:
        text = str(content or "").strip()
        if not text:
            return
        request_id = f"bg-response:{thread_id or self.session_id}"
        with self._lock:
            self._background_notice_index += 1
            block_id = f"{request_id}:{self._background_notice_index}"
            self._publish(
                "assistant_completed",
                "completed",
                block_id,
                {"text": text},
                request_id=request_id,
            )


# LLM: TuiRuntime 统一拥有 session 事件序号与 turn adapter；emit+publish 在同一锁内避免并发 seq 到达倒序。
# 类用途: 建立 TUI 状态容器、发布启动/输入事件，并为每个 request 创建唯一 adapter。
class TuiRuntime(_TuiBackgroundActivityRuntimeMixin):
    # LLM: session_id 固定一个 UI 生命周期；store 可注入用于 replay/tests，但不能在运行中替换。
    # 函数用途: 创建 TUI runtime 和全局单调 sequencer。
    def __init__(self, session_id: str, *, store: TuiStateStore | None = None) -> None:
        normalized = str(session_id or "default").strip() or "default"
        self.session_id = normalized
        self.store = store or TuiStateStore()
        self._sequencer = TuiEventSequencer(
            f"tui:session:{normalized}",
            session_id=normalized,
        )
        self._turns: dict[str, TuiTurnEventAdapter] = {}
        self._pending_steers: dict[str, str] = {}
        self._queued_prompts: dict[str, str] = {}
        self._console_index = 0
        self._background_notice_index = 0
        self._notice_text = ""
        self._notice_until = 0.0
        self._lock = threading.RLock()
        self._background_activity = _TuiBackgroundActivityController(self)

    # LLM: publish_session 只携带公开品牌/模型/目录元数据，真实配置与 secret 不进入 UI event。
    # 函数用途: 发布一次欢迎卡事件。
    def publish_session(self, *, version: str, model: str, workspace: str) -> JournalAppendResult:
        return self._publish(
            "session_started",
            "completed",
            f"session:{self.session_id}",
            {"version": version, "model": model, "workspace": workspace},
        )

    # LLM: readiness start 是从真实 Gateway wait 发出的临时 UI 事实；它不创建 turn、请求或第二个健康状态源。
    # 函数用途: 在 TUI 中开始显示 Gateway 连接动画。
    def publish_connection_check(self) -> JournalAppendResult:
        return self._publish(
            "connection_started",
            "started",
            f"connection:{self.session_id}:gateway",
            {"text": "Connecting to Gateway"},
        )

    # LLM: readiness 终态只接收布尔探测结果和错误类别；异常正文、endpoint 与 secret 不进入事件。
    # 函数用途: 结束 Gateway 连接动画，并在失败时显示可恢复错误。
    def resolve_connection_check(
        self,
        *,
        ok: bool,
        error_code: str = "",
    ) -> JournalAppendResult:
        return self._publish(
            "connection_resolved",
            "completed" if ok else "failed",
            f"connection:{self.session_id}:gateway",
            {
                "ok": bool(ok),
                "error_code": str(error_code or ""),
                "text": "" if ok else "Gateway is unavailable.",
            },
        )

    # LLM: 恢复只投影调用方已从 canonical session 加载的轮次；TUI 不读磁盘、不另存正文，也不把历史重新提交给模型。
    # 函数用途: 在欢迎卡后恢复当前会话的用户/助手可见 transcript，并为每轮生成稳定 session-local block id。
    def publish_recovered_history(
        self,
        turns: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    ) -> None:
        for index, pair in enumerate(tuple(turns or ()), start=1):
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            user_text, assistant_text = str(pair[0] or ""), str(pair[1] or "")
            history_request_id = f"history:{self.session_id}:{index}"
            if user_text:
                self._publish(
                    "user_message",
                    "completed",
                    f"{history_request_id}:user",
                    {"text": user_text},
                    request_id=history_request_id,
                )
            if assistant_text:
                self._publish(
                    "assistant_completed",
                    "completed",
                    f"{history_request_id}:assistant",
                    {"text": assistant_text},
                    request_id=history_request_id,
                )

    # LLM: enqueue_prompt 使用 request_id 作为用户块/队列身份；排队项只发 queue_added，开始执行时再原子提升为稳定用户块。
    # 函数用途: 立即显示空闲提交，或把运行中提交登记成可回取的用户队列预览。
    def enqueue_prompt(self, request_id: str, text: str, *, queued: bool) -> None:
        normalized = _required_request_id(request_id)
        if queued:
            queue_id = f"queue:{normalized}"
            with self._lock:
                self._queued_prompts[queue_id] = str(text)
                self._publish(
                    "queue_added",
                    "queued",
                    queue_id,
                    {"queue_id": queue_id, "text": str(text), "priority": "next"},
                    request_id=normalized,
                )
            return
        self._publish(
            "user_message",
            "completed",
            f"user:{normalized}",
            {"text": str(text)},
            request_id=normalized,
        )

    # LLM: Active-turn input stays in a session-local receipt map until the runtime emits the
    # same opaque client id at the real model-injection boundary; acceptance alone is not proof.
    # 函数用途: 在 Gateway 接收补充消息前先固定显示“正在插入当前任务”的等待项。
    def enqueue_active_turn_input(self, message_id: str, text: str) -> None:
        normalized = _required_message_id(message_id)
        with self._lock:
            if normalized in self._pending_steers:
                raise KeyError(f"TUI pending steer already exists: {normalized}")
            self._pending_steers[normalized] = str(text)
            self._publish(
                "steer_added",
                "queued",
                f"steer:{normalized}",
                {"message_id": normalized, "text": str(text)},
            )

    # LLM: Rejected active-turn delivery removes only the exact local receipt. The caller may
    # then enqueue one ordinary ChatJob through the existing canonical queue.
    # 函数用途: 活动回合补充未被接收时撤销等待项，允许同一正文安全回退为下一条消息。
    def cancel_active_turn_input(self, message_id: str) -> bool:
        normalized = _required_message_id(message_id)
        with self._lock:
            if normalized not in self._pending_steers:
                return False
            self._pending_steers.pop(normalized, None)
            self._publish(
                "steer_removed",
                "removed",
                f"steer:{normalized}",
                {"message_id": normalized},
            )
        return True

    # LLM: Promotion intersects runtime-confirmed ids with this TUI's local receipt map. Unknown
    # external ids are consumed as protocol events but can never create or remove local messages.
    # 函数用途: 按精确消息 ID 将真正注入模型的补充消息移入当前回合历史。
    def promote_active_turn_inputs(
        self,
        message_ids: tuple[str, ...],
        *,
        request_id: str,
    ) -> bool:
        normalized_request_id = _required_request_id(request_id)
        normalized_ids = tuple(
            dict.fromkeys(
                _required_message_id(message_id)
                for message_id in tuple(message_ids or ())
                if str(message_id or "").strip()
            )
        )
        promoted = False
        with self._lock:
            for message_id in normalized_ids:
                text = self._pending_steers.pop(message_id, None)
                if text is None:
                    continue
                self._publish(
                    "steer_promoted",
                    "completed",
                    f"user:{normalized_request_id}:steer:{message_id}",
                    {"message_id": message_id, "text": text},
                    request_id=normalized_request_id,
                )
                promoted = True
        return promoted

    # LLM: queued identity 只查 runtime 在 queue_added 时登记的 request 映射，input controller 不能按 pending 数量或文案猜测。
    # 函数用途: 判断一个尚在真实任务队列中的 request 是否属于可回取输入。
    def has_queued_prompt(self, request_id: str) -> bool:
        queue_id = f"queue:{_required_request_id(request_id)}"
        with self._lock:
            return queue_id in self._queued_prompts

    # LLM: restore_prompts 只接受已从 canonical Queue 原子移除的 request id；每项发 queue_restored 并删除同一映射。
    # 函数用途: 将尚未执行的排队消息从 TUI 预览撤下，供输入控制器恢复编辑。
    def restore_prompts(self, request_ids: tuple[str, ...]) -> None:
        normalized_ids = tuple(_required_request_id(item) for item in request_ids)
        with self._lock:
            missing = [
                request_id
                for request_id in normalized_ids
                if f"queue:{request_id}" not in self._queued_prompts
            ]
            if missing:
                raise KeyError(f"TUI queued prompts not found: {missing!r}")
            for request_id in normalized_ids:
                queue_id = f"queue:{request_id}"
                self._publish(
                    "queue_restored",
                    "restored",
                    queue_id,
                    {"queue_id": queue_id},
                    request_id=request_id,
                )
                self._queued_prompts.pop(queue_id, None)

    # LLM: legacy slash-command 输出通过这个窄入口转成 typed system block；它只携带显示文本且不解析严重级别或控制语义。
    # 函数用途: 将一条 CLI 命令结果追加到当前 TUI 对话流。
    def write_console(self, text: str) -> None:
        from .renderer import strip_ansi

        normalized = strip_ansi(str(text or "")).rstrip("\n")
        if not normalized:
            return
        with self._lock:
            self._console_index += 1
            block_id = f"console:{self.session_id}:{self._console_index}"
            self._publish(
                "system_message",
                "completed",
                block_id,
                {"text": normalized},
            )

    # LLM: notice 是有界的输入交互提示，不写事件 journal/会话历史，也不能用其文案触发退出、中断或业务状态。
    # 函数用途: 在 footer 短暂显示双击退出、清空等按键提示。
    def set_notice(self, text: str, *, duration_seconds: float = 0.8) -> None:
        normalized = str(text or "")
        with self._lock:
            self._notice_text = normalized
            self._notice_until = (
                time.monotonic() + max(0.0, float(duration_seconds or 0.0))
                if normalized
                else 0.0
            )
        self.store.invalidate()

    # LLM: notice expiry 只读 monotonic deadline 并清 UI 临时字段；不得发布 system_message 或修改 reducer status。
    # 函数用途: 返回当前尚未过期的输入提示。
    def notice(self) -> str:
        now = time.monotonic()
        with self._lock:
            if self._notice_text and now >= self._notice_until:
                self._notice_text = ""
                self._notice_until = 0.0
            return self._notice_text

    # LLM: 周期刷新只由仍会随时间改变的可见 typed 状态保活；空闲 transcript 必须停表，避免历史长度放大空转 CPU。
    # 函数用途: 告诉动画线程当前是否还有连接/思考/Compact 动画或未过期短提示需要定时重绘。
    def needs_periodic_refresh(self) -> bool:
        now = time.monotonic()
        with self._lock:
            notice_active = bool(
                self._notice_text
                and self._notice_until > 0.0
                and now < self._notice_until
            )
        if notice_active:
            return True
        snapshot = self.store.snapshot()
        return any(
            block.role in {"connection", "thinking", "compact", "background"}
            for block in snapshot.active_blocks
        )

    # LLM: begin_turn 每 request 只创建一次 adapter，重复 worker dequeue 返回同对象但不重放 turn_started。
    # 函数用途: 从队列切换为运行中回合，并启动 thinking 活动块。
    def begin_turn(self, request_id: str) -> TuiTurnEventAdapter:
        normalized = _required_request_id(request_id)
        with self._lock:
            existing = self._turns.get(normalized)
            if existing is not None:
                return existing
            queue_id = f"queue:{normalized}"
            queued_text = self._queued_prompts.get(queue_id)
            if queued_text is not None:
                self._publish(
                    "queue_promoted",
                    "removed",
                    f"user:{normalized}",
                    {"queue_id": queue_id, "text": queued_text},
                    request_id=normalized,
                )
                self._queued_prompts.pop(queue_id, None)
            adapter = TuiTurnEventAdapter(self, normalized)
            self._turns[normalized] = adapter
            adapter.start()
            return adapter

    # LLM: complete_turn 委托已存在 adapter；缺失 turn 是调用方合同错误，不静默造终态。
    # 函数用途: 发布回合 final/status/terminal 并移除活动 adapter。
    def complete_turn(self, request_id: str, summary: TuiTurnSummary) -> None:
        normalized = _required_request_id(request_id)
        with self._lock:
            adapter = self._turns.get(normalized)
        if adapter is None:
            raise KeyError(f"TUI turn not started: {normalized}")
        adapter.finalize(summary)
        with self._lock:
            self._turns.pop(normalized, None)

    # LLM: interrupt intent 只更新当前唯一活动 adapter 的显示阶段；真正取消仍由 Conversation control 和 cancellation token 执行。
    # 函数用途: 在停止请求离开 UI 线程前立即把当前回合标为正在中断。
    def request_interrupt(self) -> bool:
        with self._lock:
            active = tuple(self._turns.values())
        if not active:
            return False
        active[-1].request_interrupt()
        return True

    # LLM: selection 只更新当前 permission_id 的 UI 投影；它不能构造批准或修改 ActionPolicy binding。
    # 函数用途: 上下移动当前审批面板的选择光标。
    def move_permission_selection(self, delta: int) -> bool:
        with self._lock:
            active = tuple(self._turns.values())
        for adapter in reversed(active):
            if adapter.move_permission_selection(delta):
                return True
        return False

    # LLM: Tab intent 只路由到持有当前 permission 的 adapter；展开输入不代表允许或拒绝。
    # 函数用途: 展开或收起当前权限选项的补充说明输入。
    def toggle_permission_feedback(self) -> bool:
        with self._lock:
            active = tuple(self._turns.values())
        for adapter in reversed(active):
            if adapter.toggle_permission_feedback():
                return True
        return False

    # LLM: feedback 更新必须携带 permission_id 并只命中该活动请求，不能广播到其它回合。
    # 函数用途: 保存当前权限选项的补充说明草稿。
    def update_permission_feedback(self, permission_id: str, text: str) -> bool:
        with self._lock:
            active = tuple(self._turns.values())
        for adapter in reversed(active):
            if adapter.update_permission_feedback(permission_id, text):
                return True
        return False

    # LLM: 决定必须路由到持有同 permission_id 的活动 adapter；找不到时 fail-closed，不广播到其它回合。
    # 函数用途: 将用户的审批选择送回本地等待线程或 Gateway 决定文件。
    def resolve_permission(
        self,
        permission_id: str,
        decision: str,
        *,
        feedback: str = "",
    ) -> bool:
        with self._lock:
            active = tuple(self._turns.values())
        for adapter in reversed(active):
            if adapter.resolve_permission(permission_id, decision, feedback=feedback):
                return True
        return False

    # LLM: 统一 publish 锁住 sequencer+store，任何来源都不能让较大 seq 先进入 journal。
    # 函数用途: 生成并发布一条 session stream TuiEvent。
    def _publish(
        self,
        kind: str,
        phase: str,
        block_id: str,
        payload: dict[str, Any] | None = None,
        *,
        request_id: str = "",
    ) -> JournalAppendResult:
        with self._lock:
            event = self._sequencer.emit(
                kind,
                phase,
                block_id,
                payload,
                request_id=request_id,
                turn_id=request_id,
            )
            return self.store.publish(event)


# LLM: TuiTurnEventAdapter 实现 callable/write_model/write_progress/Gateway event 四个入口，共享同一 typed 生命周期和 block identity。
# 类用途: 把一次本地或 Gateway 回合的流式输出、工具事件和终态写入 TuiRuntime。
class TuiTurnEventAdapter:
    # LLM: adapter 状态只记当前显示 block/id/text 和已见工具，不复制 agent/tool runtime 业务状态。
    # 函数用途: 初始化一个尚未开始的回合 adapter。
    def __init__(self, runtime: TuiRuntime, request_id: str) -> None:
        self.runtime = runtime
        self.request_id = request_id
        self.turn_block_id = f"turn:{request_id}"
        self.thinking_block_id = f"thinking:{request_id}:0"
        self._thinking_active = False
        self._thinking_index = 0
        self._thinking_text = ""
        self._thinking_started_at = 0.0
        self._compact_active = False
        self._compact_block_id = ""
        self._compact_payload: dict[str, object] = {}
        self._assistant_index = 0
        self._assistant_block_id = ""
        self._assistant_text = ""
        self._terminal_response_text = ""
        self._output_chars = 0
        self._output_bytes = 0
        self._tool_blocks: set[str] = set()
        self._runtime_progress_index = 0
        self._interrupt_requested = False
        self._finished = False
        self._lock = threading.RLock()
        self._permissions = _TuiPermissionController(self)

    # LLM: start 只允许一次，并先发布 turn_started 再发布 thinking_started，顺序由 runtime 全局 seq 保证。
    # 函数用途: 启动回合与等待模型的活动块。
    def start(self) -> None:
        with self._lock:
            if self._thinking_active or self._finished:
                return
            self.runtime._publish(
                "turn_started",
                "started",
                self.turn_block_id,
                {"activity": "Thinking"},
                request_id=self.request_id,
            )
            self._thinking_started_at = time.time()
            self.runtime._publish(
                "thinking_started",
                "started",
                self.thinking_block_id,
                {"started_at": self._thinking_started_at},
                request_id=self.request_id,
            )
            self._thinking_active = True

    # LLM: 此事件只确认用户已发出 typed interrupt intent，不提前伪造后端终态；重复按键可幂等重发控制但只发布一次显示状态。
    # 函数用途: 立即把活动回合切换为 Interrupting，等待 worker 的真实 interrupted/completed 终态。
    def request_interrupt(self) -> None:
        with self._lock:
            if self._finished or getattr(self, "_interrupt_requested", False):
                return
            self._interrupt_requested = True
            self.runtime._publish(
                "turn_interrupt_requested",
                "updated",
                self.turn_block_id,
                {"activity": "Interrupting"},
                request_id=self.request_id,
            )

    # LLM: Gateway sink 只负责原子写决定文件，request/decision 都已由 shared approval contract 校验；本地模式保持 None。
    # 函数用途: 为当前 Gateway 回合安装审批答复写回函数。
    def configure_gateway_permission_sink(
        self,
        sink: Callable[[ToolApprovalRequest, ToolApprovalDecision], None],
    ) -> None:
        self._permissions.configure_gateway_sink(sink)

    # LLM: 清理仅移除跨进程 sink；尚未收口的 pending request 会在 finalize 中按 cancelled 关闭，不能静默丢失。
    # 函数用途: 在 Gateway 轮询结束后解除审批文件写回函数。
    def clear_gateway_permission_sink(self) -> None:
        self._permissions.clear_gateway_sink()

    # LLM: 本地 ToolExecutor 在该方法内等待 UI typed decision；取消令牌可打断等待，批准仍只由精确 request binding 生成。
    # 函数用途: 发布本地工具审批面板并阻塞到用户选择或取消。
    def request_permission(
        self,
        request_value: Mapping[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> dict[str, object]:
        return self._permissions.request_permission(
            request_value,
            cancellation_token=cancellation_token,
        )

    # LLM: selection 事件必须命中当前 pending id 并在 options 范围内环绕；不存在面板时不产生 journal 噪声。
    # 函数用途: 移动当前审批选项并请求重绘。
    def move_permission_selection(self, delta: int) -> bool:
        return self._permissions.move_selection(delta)

    # LLM: 只有声明 feedback_type 的当前选项支持 Tab；模式变化保留该选项自己的草稿但不修改审批决定。
    # 函数用途: 切换当前 Yes/No 选项的补充说明输入模式。
    def toggle_permission_feedback(self) -> bool:
        return self._permissions.toggle_feedback()

    # LLM: 文本草稿按 option id 隔离，且只在该 permission 的 feedback mode 中接受；文案本身没有控制权。
    # 函数用途: 更新当前权限选择的补充说明文本。
    def update_permission_feedback(self, permission_id: str, text: str) -> bool:
        return self._permissions.update_feedback(permission_id, text)

    # LLM: decision 必须属于 request 暴露的 option；外部 sink 成功后才关闭 overlay，本地等待者通过同一 Event 被唤醒。
    # 函数用途: 处理 Enter/Esc/Ctrl-C 产生的审批选择。
    def resolve_permission(
        self,
        permission_id: str,
        decision_value: str,
        *,
        feedback: str = "",
    ) -> bool:
        return self._permissions.resolve(
            permission_id,
            decision_value,
            feedback=feedback,
        )

    # LLM: callable 兼容 backend on_chunk；所有模型正文仍统一走 write_model。
    # 函数用途: 接收一个模型文本增量。
    def __call__(self, chunk: str) -> bool:
        return self.write_model(chunk)

    # LLM: 模型 delta 创建/追加 assistant block；turn activity 持续到终态，renderer 在可见 stream 期间按 typed block 隐藏 spinner。
    # 函数用途: 发布助手文本增量、刷新输出 token 估计并返回是否有可见内容。
    def write_model(self, chunk: str) -> bool:
        text = str(chunk or "")
        if not text:
            return False
        with self._lock:
            self._ensure_assistant_started()
            self.runtime._publish(
                "assistant_delta",
                "delta",
                self._assistant_block_id,
                {"text": text},
                request_id=self.request_id,
            )
            self._assistant_text += text
            self._record_output_metrics(text)
        return bool(text.strip())

    # LLM: This callback is invoked only at the runtime's real guidance-injection boundary. It
    # closes any preceding assistant segment, then promotes the exact correlated client ids.
    # 函数用途: 确认补充消息已经进入当前模型回合，并把等待提示变成正式用户消息。
    def begin_active_turn_input(self, client_message_ids: tuple[str, ...]) -> None:
        with self._lock:
            self._complete_active_assistant()
            self.runtime.promote_active_turn_inputs(
                client_message_ids,
                request_id=self.request_id,
            )

    # LLM: rich thinking 必须来自上游显式 write_thinking/assistant_thinking 事件；这里不读取 assistant 正文猜测思考，也不接收签名或 redacted payload。
    # 函数用途: 把一次模型调用的完整思考保存成默认折叠、可用 Ctrl+O 展开的独立块。
    def write_thinking(self, text: str, *, duration_seconds: float = 0.0) -> bool:
        return _publish_turn_thinking(self, text, duration_seconds=duration_seconds)

    # LLM: thinking_delta 是 gateway 流式思考增量；首个增量复用 turn 的活动 thinking 块，
    # 之后逐块追加（Ctrl+O 展开可见实时内容），思考结束后由 thinking_completed 冻结。
    # 函数用途: 把一次模型调用的流式思考增量追加到活动 thinking 块。
    def write_thinking_delta(self, text: str) -> bool:
        content = str(text or "")
        if not content:
            return False
        with self._lock:
            if not self._thinking_active:
                return False
            self._thinking_text += content
            self.runtime._publish(
                "thinking_delta",
                "delta",
                self.thinking_block_id,
                {"text": content},
                request_id=self.request_id,
            )
        return True

    # LLM: 完整 thinking 事件到达时若流式增量块已活动，直接冻结（完整文本覆盖增量，
    # 不重复建块）；无活动块时按旧契约一次性 started+completed。
    # 函数用途: 收口一次模型调用的思考块（流式增量或一次性全文）。
    def finalize_thinking(self, text: str, *, duration_seconds: float = 0.0) -> bool:
        with self._lock:
            if self._thinking_active:
                content = str(text or "")
                if content:
                    self._thinking_text = content
                self.runtime._publish(
                    "thinking_completed",
                    "completed",
                    self.thinking_block_id,
                    {
                        "text": self._thinking_text,
                        "duration_seconds": max(0.0, float(duration_seconds or 0.0)),
                    },
                    request_id=self.request_id,
                )
                self._thinking_active = False
                self._thinking_text = ""
                return True
        return _publish_turn_thinking(self, text, duration_seconds=duration_seconds)

    # LLM: Context usage is a status-only typed projection. It must not create transcript blocks,
    # parse labels, or retain any provider-visible content beyond the numeric whitelist.
    # 函数用途: 接收本地模型调用产生的实时上下文快照并刷新输入框上方状态条。
    def write_context_usage(self, usage: Mapping[str, object]) -> bool:
        payload = _tui_context_usage_payload(usage)
        if not payload:
            return False
        with self._lock:
            self.runtime._publish(
                "status_updated",
                "updated",
                f"status:{self.request_id}",
                {
                    "context_tokens": payload["current_tokens"],
                    "context_usage": payload,
                },
                request_id=self.request_id,
            )
        return True

    # LLM: A mid-turn context compaction becomes one stable, content-free system event. It does
    # not advance durable conversation compact_generation or infer anything from display text.
    # 函数用途: 在当前回合留下“裁剪前后 token 与工具对数”的可展开历史提示。
    def write_context_compaction(self, value: Mapping[str, object]) -> bool:
        payload = _tui_context_compaction_payload(value)
        if not payload:
            return False
        with self._lock:
            generation = payload["generation"]
            self.runtime._publish(
                "context_window_compacted",
                "completed",
                f"context-window:{self.request_id}:{generation}",
                payload,
                request_id=self.request_id,
            )
        return True

    # LLM: 会话 Compact 块只消费冻结 schema 的顺序事件，不根据显示文案推断开始、完成或失败。
    # 函数用途: 将 Gateway 传来的持久会话 Compact 阶段更新为一个原位进度块。
    def write_conversation_compact_progress(
        self,
        value: Mapping[str, object],
    ) -> bool:
        payload = _tui_conversation_compact_progress_payload(value)
        if not payload:
            return False
        phase = str(payload["phase"])
        generation = int(payload["generation"])
        block_id = f"conversation-compact:{self.request_id}:{generation}"
        with self._lock:
            if phase == "started":
                if self._compact_active:
                    return False
                self._compact_active = True
                self._compact_block_id = block_id
                self._compact_payload = dict(payload)
                self.runtime._publish(
                    "conversation_compaction_started",
                    "started",
                    block_id,
                    payload,
                    request_id=self.request_id,
                )
                return True
            if not self._compact_active or self._compact_block_id != block_id:
                return False
            self._compact_payload = dict(payload)
            if phase == "progress":
                kind, event_phase = "conversation_compaction_progress", "updated"
            elif phase == "completed":
                kind, event_phase = "conversation_compaction_completed", "completed"
            else:
                kind, event_phase = "conversation_compaction_failed", "failed"
            self.runtime._publish(
                kind,
                event_phase,
                block_id,
                payload,
                request_id=self.request_id,
            )
            if phase in {"completed", "failed"}:
                self._compact_active = False
                self._compact_block_id = ""
                self._compact_payload = {}
            return True

    # LLM: progress 只读取结构化 event；legacy_text 参数为旧调用兼容但不得参与 phase/tool/id 决策。
    # 函数用途: 发布工具开始、进度和终态，并在工具前冻结助手 commentary、保留全局活动 spinner。
    def write_progress(self, event: dict[str, object], legacy_text: str = "") -> None:
        del legacy_text
        progress = dict(event or {})
        with self._lock:
            self._complete_active_assistant(process=True)
            self._publish_tool_progress(progress)

    # LLM: Gateway typed row 返回是否已消费；未知 kind 留给 shared projector fail-closed，不读取 row text 猜类型。
    # 函数用途: 接收一个 Gateway chunk object 并映射为 TUI event。
    def on_gateway_event(self, payload: dict[str, Any]) -> bool:
        return _consume_gateway_turn_event(self, payload)

    # LLM: finalize 只使用结构化 summary，保证 active block 先 terminal，再 status，最后 turn terminal 且仅一次。
    # 函数用途: 完成、失败或中断当前回合。
    def finalize(self, summary: TuiTurnSummary) -> None:
        with self._lock:
            if self._finished:
                return
            self._permissions.cancel_pending()
            self._close_compact_if_active(
                phase="interrupted" if summary.interrupted else "failed"
            )
            self._complete_thinking()
            if summary.ok and summary.response_text:
                self._finalize_assistant_text(summary.response_text)
            else:
                self._complete_active_assistant(phase="interrupted" if summary.interrupted else "failed")
            self.runtime._publish(
                "status_updated",
                "updated",
                f"status:{self.request_id}",
                {
                    "context_tokens": summary.context_tokens,
                    "output_tokens": summary.output_tokens,
                    "tool_rounds": summary.tool_rounds,
                },
                request_id=self.request_id,
            )
            self._publish_turn_terminal(summary)
            self._finished = True

    # LLM: 回合终态不得遗留活动 Compact 块；这里只收口展示状态，不修改会话摘要或代际。
    # 函数用途: 在回合异常结束时把未收到终态的 Compact 进度块标记为失败/中断。
    def _close_compact_if_active(self, *, phase: str) -> None:
        if not self._compact_active or not self._compact_block_id:
            return
        payload = {
            **self._compact_payload,
            "phase": "failed",
            "stage": "failed",
        }
        self.runtime._publish(
            "conversation_compaction_failed",
            "interrupted" if phase == "interrupted" else "failed",
            self._compact_block_id,
            payload,
            request_id=self.request_id,
        )
        self._compact_active = False
        self._compact_block_id = ""
        self._compact_payload = {}

    # LLM: thinking terminal 携带完整思考文本与真实耗时，renderer 折叠显示
    # “Thought for Xs”并允许 Ctrl+O 展开；无思考内容时与旧行为一致（不留下空历史块）。
    # 函数用途: 关闭当前等待模型的 thinking block。
    def _complete_thinking(self) -> None:
        if not self._thinking_active:
            return
        elapsed = max(0.0, time.time() - self._thinking_started_at)
        self.runtime._publish(
            "thinking_completed",
            "completed",
            self.thinking_block_id,
            {
                "text": self._thinking_text,
                "duration_seconds": round(elapsed, 1),
            },
            request_id=self.request_id,
        )
        self._thinking_active = False
        self._thinking_text = ""

    # LLM: streaming token 是 UI 临时估计，只累计已发布 assistant delta 的字符/字节并写 typed status；终态精确 summary 可覆盖。
    # 函数用途: 更新当前回合的保守输出 token 计数，让长回合状态行即时可见。
    def _record_output_metrics(self, text: str) -> None:
        self._output_chars += len(text)
        self._output_bytes += len(text.encode("utf-8"))
        estimated_tokens = max(
            1,
            (self._output_chars + 2) // 3,
            (self._output_bytes + 2) // 3,
        )
        self.runtime._publish(
            "status_updated",
            "updated",
            f"status:{self.request_id}",
            {"output_tokens": estimated_tokens},
            request_id=self.request_id,
        )

    # LLM: assistant identity 按回合内 segment index 生成，工具前后的模型 commentary 不会合并成同一块。
    # 函数用途: 确保当前存在一个活动助手块。
    def _ensure_assistant_started(self) -> None:
        if self._assistant_block_id:
            return
        self._assistant_index += 1
        self._assistant_block_id = f"assistant:{self.request_id}:{self._assistant_index}"
        self._assistant_text = ""
        self.runtime._publish(
            "assistant_started",
            "started",
            self._assistant_block_id,
            request_id=self.request_id,
        )

    # LLM: complete 只冻结当前 segment 一次；phase 显式传入且不得由文本内容决定。
    # process 标记表示该段是工具边界前的模型过程说明，renderer 据此折叠展示。
    # 函数用途: 结束活动助手块（可选归为可折叠过程）并清空 adapter 指针。
    def _complete_active_assistant(
        self,
        *,
        phase: str = "completed",
        process: bool = False,
    ) -> None:
        if not self._assistant_block_id:
            return
        payload: dict[str, Any] = {"text": self._assistant_text}
        if process:
            payload["process"] = True
        self.runtime._publish(
            "assistant_completed",
            phase,
            self._assistant_block_id,
            payload,
            request_id=self.request_id,
        )
        self._assistant_block_id = ""
        self._assistant_text = ""

    # LLM: committed final 采用 summary 完整正文覆盖 active delta；无 active 时创建并立即冻结新 segment。
    # 函数用途: 发布最终助手正文且不产生重复块。
    def _finalize_assistant_text(self, text: str) -> None:
        if self._terminal_response_text == str(text):
            return
        self._ensure_assistant_started()
        self.runtime._publish(
            "assistant_completed",
            "completed",
            self._assistant_block_id,
            {"text": str(text)},
            request_id=self.request_id,
        )
        self._assistant_block_id = ""
        self._assistant_text = ""

    # LLM: tool block id 只使用结构化 round/call_index/request，unknown tool 仍有稳定开放名称。
    # 函数用途: 将一个 structured progress 映射为工具 block 事件。
    def _publish_tool_progress(self, progress: dict[str, Any]) -> None:
        block_id = _tool_block_id(self.request_id, progress)
        phase = str(progress.get("phase") or "updated").strip().lower()
        if block_id not in self._tool_blocks:
            self.runtime._publish(
                "tool_started",
                "started",
                block_id,
                _tool_payload(progress),
                request_id=self.request_id,
            )
            self._tool_blocks.add(block_id)
        terminal_kind, terminal_phase = _tool_terminal(phase, progress)
        if terminal_kind:
            self.runtime._publish(
                terminal_kind,
                terminal_phase,
                block_id,
                _tool_payload(progress),
                request_id=self.request_id,
            )
            return
        self.runtime._publish(
            "tool_progress",
            "updated",
            block_id,
            _tool_payload(progress),
            request_id=self.request_id,
        )

    # LLM: verbose runtime progress 只在显式 full 时显示为 system block，off/on 行仍由上游策略隐藏。
    # 函数用途: 处理 Gateway runtime_progress 并返回是否消费。
    def _publish_runtime_progress(self, payload: dict[str, Any]) -> bool:
        if str(payload.get("verbose_level") or "off").lower() != "full":
            return True
        text = str(payload.get("text") or "")
        if text:
            self._runtime_progress_index += 1
            self.runtime._publish(
                "system_message",
                "completed",
                f"runtime:{self.request_id}:{self._runtime_progress_index}",
                {"text": text},
                request_id=self.request_id,
            )
        return True

    # LLM: turn 终态仅由 summary booleans 映射，error 文案只作为 error block 内容。
    # 函数用途: 发布回合成功、失败或中断事件。
    def _publish_turn_terminal(self, summary: TuiTurnSummary) -> None:
        if summary.interrupted:
            kind, phase = "turn_interrupted", "interrupted"
        elif summary.ok:
            kind, phase = "turn_completed", "completed"
        else:
            kind, phase = "turn_failed", "failed"
        if summary.interrupted:
            self.runtime._publish(
                "interrupt_notice",
                "completed",
                f"interrupt:{self.request_id}",
                {"text": "Interrupted · What should my-agent do instead?"},
                request_id=self.request_id,
            )
        if summary.error:
            self.runtime._publish(
                "system_message",
                "failed",
                f"error:{self.request_id}",
                {"text": summary.error, "severity": "error"},
                request_id=self.request_id,
            )
        self.runtime._publish(kind, phase, self.turn_block_id, request_id=self.request_id)


# LLM: Gateway row dispatch stays a pure adapter helper over one TuiTurnEventAdapter instance;
# moving it outside the class is size governance and must not create another handler registry.
# 函数用途: 按结构化 kind 将一条 Gateway 事件交给当前回合的唯一 typed 入口。
def _consume_gateway_turn_event(
    adapter: TuiTurnEventAdapter,
    payload: dict[str, Any],
) -> bool:
    kind = str(payload.get("kind") or "")
    if kind == "tool_progress" and isinstance(payload.get("progress"), dict):
        adapter.write_progress(dict(payload["progress"]))
        return True
    if kind == "model_delta":
        adapter.write_model(str(payload.get("text") or ""))
        return True
    if kind == "assistant_commentary":
        # 新 Gateway 已实时流式 model_delta，此事件只是真实工具边界的冻结标记：
        # 把当前候选段归为可折叠过程。兼容旧 Gateway（无 model_delta 事件）时，
        # 无活动块说明正文整段尚未到达，仍需按旧契约落地全文。
        with adapter._lock:
            if adapter._assistant_block_id:
                adapter._complete_active_assistant(process=True)
            else:
                adapter.write_model(str(payload.get("text") or ""))
                adapter._complete_active_assistant(process=True)
        return True
    if kind == "assistant_thinking":
        # 完整 thinking 事件：冻结流式增量块（若有），否则按旧契约一次性建块。
        return adapter.finalize_thinking(
            str(payload.get("text") or ""),
            duration_seconds=float(payload.get("duration_seconds") or 0.0),
        )
    if kind == "thinking_delta":
        return adapter.write_thinking_delta(str(payload.get("text") or ""))
    if kind == "assistant_final":
        final_text = str(payload.get("text") or "")
        adapter.write_model(final_text)
        with adapter._lock:
            adapter._complete_active_assistant()
            adapter._terminal_response_text = final_text
        return True
    if kind == "runtime_progress":
        return adapter._publish_runtime_progress(payload)
    if kind == "active_turn_input_consumed":
        raw_message_ids = payload.get("client_message_ids")
        message_ids = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in (
                    raw_message_ids
                    if isinstance(raw_message_ids, (list, tuple))
                    else ()
                )
                if str(value or "").strip()
            )
        )
        adapter.begin_active_turn_input(message_ids)
        return True
    if kind == "context_usage_updated" and isinstance(
        payload.get("context_usage"),
        Mapping,
    ):
        return adapter.write_context_usage(payload["context_usage"])
    if kind == "context_window_compacted" and isinstance(
        payload.get("context_compaction"),
        Mapping,
    ):
        return adapter.write_context_compaction(payload["context_compaction"])
    if kind == "conversation_compaction_progress" and isinstance(
        payload.get("compact_progress"),
        Mapping,
    ):
        return adapter.write_conversation_compact_progress(payload["compact_progress"])
    if kind == "conversation_compacted":
        generation = _nonnegative_int(payload.get("compact_generation"))
        if generation <= 0:
            return False
        adapter.runtime._publish(
            "compact_boundary",
            "completed",
            f"compact:{adapter.request_id}:{generation}",
            {"compact_generation": generation},
            request_id=adapter.request_id,
        )
        return True
    if kind == "permission_requested" and isinstance(payload.get("permission"), dict):
        adapter._permissions.open(ToolApprovalRequest.from_mapping(payload["permission"]))
        return True
    if kind == "permission_resolved":
        return adapter._permissions.accept_gateway_resolution(payload)
    return False


# LLM: rich thinking helper 只写 adapter 自身的显示序号和 runtime journal；拆出类体是尺寸治理，不创建第二套状态或事件入口。
# 函数用途: 发布一次完整思考的 started/completed 事件，并给每轮思考分配稳定块编号。
def _publish_turn_thinking(
    adapter: TuiTurnEventAdapter,
    text: str,
    *,
    duration_seconds: float,
) -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    with adapter._lock:
        adapter._thinking_index += 1
        block_id = f"thinking:{adapter.request_id}:{adapter._thinking_index}"
        payload = {
            "text": content,
            "duration_seconds": max(0.0, float(duration_seconds or 0.0)),
        }
        adapter.runtime._publish(
            "thinking_started",
            "started",
            block_id,
            request_id=adapter.request_id,
        )
        adapter.runtime._publish(
            "thinking_completed",
            "completed",
            block_id,
            payload,
            request_id=adapter.request_id,
        )
    return True


# LLM: request id 是所有回合 block/queue 的 canonical 显示关联，空值必须在入口失败。
# 函数用途: 规范并验证 request id。
def _required_request_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("TUI request_id is required")
    return normalized


# LLM: Client message ids are opaque correlation facts; validation checks presence only and
# never derives identity from user text, sequence position, or request count.
# 函数用途: 校验活动回合补充消息的客户端关联 ID。
def _required_message_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("TUI active-turn message_id is required")
    return normalized


# LLM: 工具 block identity 只取 typed round/call_index，不使用 detail/output 文本。
# 函数用途: 生成工具显示块稳定 id。
def _tool_block_id(request_id: str, progress: dict[str, Any]) -> str:
    return (
        f"tool:{request_id}:"
        f"{_nonnegative_int(progress.get('round'))}:"
        f"{_nonnegative_int(progress.get('call_index'))}"
    )


# LLM: tool payload 白名单保持与 reducer public metadata 对齐，不复制 legacy_text 或未知内部字段。
# 函数用途: 生成工具显示事件 payload。
def _tool_payload(progress: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "tool",
        "round",
        "call_index",
        "phase",
        "status",
        "detail",
        "output",
        "display",
        "ok",
        "handler_executed",
        "duration_ms",
        "failure_stage",
        "error_code",
        # S-TP1（真机实锤）：gateway 在 task_progress 工具 ok 时附的 items 快照，
        # 白名单漏掉导致 TUI todo 面板永远收不到数据。必须与 reducer 的
        # _update_todo_block 消费端对齐。
        "task_progress_items",
    }
    payload = {key: progress[key] for key in allowed if key in progress}
    if str(progress.get("phase") or "").strip().lower() == "started" and progress.get("detail"):
        payload["invocation"] = progress["detail"]
    return payload


# LLM: terminal 映射只认 typed phase/ok，未知 phase 保持 progress，不根据本地化 status 文案判断。
# 函数用途: 返回工具 terminal kind/phase，非终态返回空字符串。
def _tool_terminal(phase: str, progress: dict[str, Any]) -> tuple[str, str]:
    if phase in {"interrupted", "cancelled"}:
        return "tool_failed", "interrupted"
    if phase not in {"finished", "completed", "succeeded", "failed"}:
        return "", ""
    ok = bool(progress.get("ok")) if "ok" in progress else phase not in {"failed"}
    return ("tool_completed", "completed") if ok else ("tool_failed", "failed")


# LLM: 工具序号畸形时归零只影响显示 identity，不授权或选择任何工具。
# 函数用途: 安全读取非负整数。
def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Direct local and Gateway context events converge through this exact schema/field whitelist;
# arbitrary nested values cannot reach reducer snapshots or renderer cache keys.
# 函数用途: 清洗上下文状态事件，只保留 TUI 展示需要的非负数字与协议名。
def _tui_context_usage_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    if value.get("schema") != "model_visible_context_usage.v1":
        return {}
    token_fields = (
        "context_window_tokens",
        "compact_trigger_tokens",
        "current_tokens",
        "prompt_tokens",
        "messages_tokens",
        "runtime_guidance_tokens",
        "tool_schema_tokens",
    )
    protocol = str(value.get("protocol") or "unknown")
    return {
        "schema": "model_visible_context_usage.v1",
        "estimated": value.get("estimated") is True,
        **{field: _nonnegative_int(value.get(field)) for field in token_fields},
        "protocol": protocol if protocol in {"native", "text"} else "unknown",
    }


# LLM: Direct and Gateway compaction paths converge through one exact counter whitelist; this
# event is display evidence only and cannot mutate conversation compact authority.
# 函数用途: 校验活动回合上下文裁剪事件并丢弃任何未知正文或字段。
def _tui_context_compaction_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    if value.get("schema") != "model_visible_context_compaction.v1":
        return {}
    fields = (
        "generation",
        "before_tokens",
        "after_tokens",
        "trigger_tokens",
        "dropped_pairs",
        "preserved_pairs",
    )
    payload = {
        "schema": "model_visible_context_compaction.v1",
        **{field: _nonnegative_int(value.get(field)) for field in fields},
    }
    return payload if payload["generation"] > 0 else {}


# LLM: TUI 在 Gateway 清洗后仍重新验证 Compact phase/stage/计数，直连和 replay 不能绕过展示边界。
# 函数用途: 将合法会话 Compact 进度转成 reducer 可用的有界数字快照。
def _tui_conversation_compact_progress_payload(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    if value.get("schema") != "conversation_compaction_progress.v1":
        return {}
    phase = str(value.get("phase") or "")
    stage = str(value.get("stage") or "")
    if phase not in {"started", "progress", "completed", "failed"}:
        return {}
    if stage not in {
        "preparing",
        "summarizing",
        "measuring",
        "checkpointing",
        "committing",
        "completed",
        "failed",
    }:
        return {}
    generation = _nonnegative_int(value.get("generation"))
    if generation <= 0:
        return {}
    return {
        "schema": "conversation_compaction_progress.v1",
        "phase": phase,
        "stage": stage,
        "percent": min(100, _nonnegative_int(value.get("percent"))),
        "generation": generation,
        "before_tokens": _nonnegative_int(value.get("before_tokens")),
        "after_tokens": _nonnegative_int(value.get("after_tokens")),
        "trigger_tokens": _nonnegative_int(value.get("trigger_tokens")),
        "source_messages": _nonnegative_int(value.get("source_messages")),
    }


# LLM: 本地审批等待只读 cancellation_token 的结构化状态；取消原因文案不参与控制判断。
# 函数用途: 判断等待审批的回合是否已被取消。
def _cancellation_requested(token: object | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    if callable(checker):
        return bool(checker())
    return bool(getattr(token, "cancelled", False))


__all__ = ["TuiRuntime", "TuiTurnEventAdapter", "TuiTurnSummary"]
