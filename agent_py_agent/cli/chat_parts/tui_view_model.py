# LLM: 本模块是 typed TUI event 到可渲染快照的唯一 reducer；不得读取模型正文猜状态，也不得执行工具、权限或会话动作。
# 模块用途: 管理稳定历史、候选块与界面状态；canonical final 按精确 ID 接替候选，流关闭不决定任务终态。

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .tui_events import JournalAppendResult, TuiEvent, TuiEventJournal

TERMINAL_BLOCK_PHASES = frozenset({"completed", "failed", "interrupted"})
_LOGGER = logging.getLogger(__name__)


# LLM: TuiBlock 是纯显示投影，metadata 只能携带脱敏/有界字段和 canonical refs，不能成为业务状态源。
# 类用途: 表示用户、助手、思考、工具、系统或错误的一块终端内容。
@dataclass(frozen=True)
class TuiBlock:
    block_id: str
    kind: str
    role: str
    phase: str
    text: str = ""
    title: str = ""
    detail: str = ""
    created_seq: int = 0
    updated_seq: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# LLM: TuiQueuedInput 保存输入队列的 UI 事实，真实执行顺序仍由同一 canonical prompt queue 驱动。
# 类用途: 展示一条已排队、可弹回编辑的用户输入。
@dataclass(frozen=True)
class TuiQueuedInput:
    queue_id: str
    text: str
    priority: str
    seq: int


# LLM: TuiPendingSteer is a UI-only receipt keyed by the opaque client message id; the text
# becomes stable history only after the active runtime reports that exact id was injected.
# 类用途: 展示一条正在送入当前任务、但尚未收到模型注入确认的用户补充消息。
@dataclass(frozen=True)
class TuiPendingSteer:
    message_id: str
    text: str
    seq: int


# LLM: TuiPermissionOverlay 只投影 ActionPolicy 请求；decision 必须由 typed intent 送回授权主链。
# 类用途: 描述当前底部权限面板的标题、选项和绑定身份。
@dataclass(frozen=True)
class TuiPermissionOverlay:
    permission_id: str
    block_id: str
    title: str
    description: str
    options: tuple[dict[str, Any], ...]
    selected_index: int = 0
    feedback_mode: bool = False
    feedback: str = ""
    feedback_placeholder: str = ""


# LLM: This is a display-only copy of model_visible_context_usage.v1. It contains numeric
# estimates only; provider content and compact authority remain in the Agent runtime.
# 类用途: 保存最近一次真实模型调用前的上下文总量、窗口、压缩线和分类估算。
@dataclass(frozen=True)
class TuiContextUsage:
    current_tokens: int = 0
    context_window_tokens: int = 0
    compact_trigger_tokens: int = 0
    prompt_tokens: int = 0
    messages_tokens: int = 0
    runtime_guidance_tokens: int = 0
    tool_schema_tokens: int = 0
    estimated: bool = True
    protocol: str = "unknown"


# LLM: TuiStatus 是 footer/spinner/context strip 的结构化快照；elapsed 等展示值由 renderer 的 clock 计算，不写回状态机。
# 类用途: 保存当前 turn 活动、统计、实时上下文、提示和模式。
@dataclass(frozen=True)
class TuiStatus:
    phase: str = "idle"
    activity: str = ""
    started_at: float = 0.0
    last_event_at: float = 0.0
    context_tokens: int = 0
    output_tokens: int = 0
    tool_rounds: int = 0
    compact_count: int = 0
    mode: str = "default"
    context_usage: TuiContextUsage | None = None


# LLM: TuiDiagnostic 记录被拒绝/未知/非法事件的有界机器事实，默认不作为用户消息显示。
# 类用途: 为测试和 debug 提供 reducer 失败原因。
@dataclass(frozen=True)
class TuiDiagnostic:
    code: str
    event_id: str
    kind: str
    block_id: str


# LLM: TuiViewSnapshot 是 UI 线程读取的不可变投影；活动谓词只供显示/刷新，不改 Todo 或运行账，调用方不得修改 reducer。
# 类用途: 一次性取得历史、活动、队列和确认后的模型显示名；这些投影不改变执行配置。
@dataclass(frozen=True)
class TuiViewSnapshot:
    stable_blocks: tuple[TuiBlock, ...]
    active_blocks: tuple[TuiBlock, ...]
    pending_steers: tuple[TuiPendingSteer, ...]
    queued_inputs: tuple[TuiQueuedInput, ...]
    permission: TuiPermissionOverlay | None
    status: TuiStatus
    diagnostics: tuple[TuiDiagnostic, ...]
    background_sync_failed: bool = False
    selected_model_name: str = ""

    # LLM: Display activity comes from the current turn phase or canonical background count,
    # never from unfinished Todo items; renderer and refresh cadence must share this predicate.
    # 函数用途: 判断页面是否仍有前台或后台工作，避免模型漏勾清单导致终态页面永远闪动；不改变业务状态。
    @property
    def has_active_work(self) -> bool:
        return self.status.phase in {"running", "interrupting"} or any(
            block.role == "background"
            and block.phase not in TERMINAL_BLOCK_PHASES
            and _nonnegative_int(block.metadata.get("active_task_count"), 0) > 0
            for block in self.active_blocks
        )


# LLM: 该 mixin 只实现权限 overlay 的 typed 状态转换，由唯一 TuiViewModelReducer 继承；不得独立实例化或持有第二份状态。
# 类用途: 将权限请求、选项、反馈和结束事件分组维护，避免主 reducer 类体过长。
class _TuiPermissionReducerMixin:
    permission: TuiPermissionOverlay | None
    active_blocks: dict[str, TuiBlock]

    # LLM: 权限请求必须带 permission_id/options 并绑定 active tool block，缺失时不显示空壳面板。
    # 函数用途: 打开底部权限 overlay，并把工具块标成 waiting_permission。
    def _handle_permission_requested(self, event: TuiEvent) -> None:
        permission_id = str(event.payload.get("permission_id") or "").strip()
        options = event.payload.get("options")
        if not permission_id or not isinstance(options, (list, tuple)):
            raise ValueError("permission_id/options required")
        public_options = tuple(dict(item) for item in options if isinstance(item, dict))
        if not public_options:
            raise ValueError("permission options required")
        selected_index = min(
            len(public_options) - 1,
            max(0, int(event.payload.get("selected_index") or 0)),
        )
        self.permission = TuiPermissionOverlay(
            permission_id=permission_id,
            block_id=event.block_id,
            title=str(event.payload.get("title") or "Tool use"),
            description=str(event.payload.get("description") or ""),
            options=public_options,
            selected_index=selected_index,
            feedback_placeholder=str(
                public_options[selected_index].get("feedback_placeholder") or ""
            ),
        )
        block = self.active_blocks.get(event.block_id)
        if block is not None:
            self.active_blocks[event.block_id] = replace(
                block,
                phase="waiting_permission",
                updated_seq=event.seq,
            )

    # LLM: selection 只允许更新同 permission_id 且落在 options 范围内；迟到或越界事件不能改变授权面板。
    # 函数用途: 更新审批面板当前高亮选项。
    def _handle_permission_selection_changed(self, event: TuiEvent) -> None:
        permission_id = str(event.payload.get("permission_id") or "").strip()
        if self.permission is None or self.permission.permission_id != permission_id:
            self.record_diagnostic("PERMISSION_SELECTION_MISMATCH", event)
            return
        selected_index = int(event.payload.get("selected_index") or 0)
        if selected_index < 0 or selected_index >= len(self.permission.options):
            self.record_diagnostic("PERMISSION_SELECTION_OUT_OF_RANGE", event)
            return
        self.permission = replace(
            self.permission,
            selected_index=selected_index,
            feedback_mode=bool(event.payload.get("feedback_mode")),
            feedback=str(event.payload.get("feedback") or ""),
            feedback_placeholder=str(event.payload.get("feedback_placeholder") or ""),
        )

    # LLM: feedback mode 只能修改同一 pending permission 的当前选项投影；它不改变 decision 或产生授权。
    # 函数用途: 展开或收起当前权限选项的补充说明输入框。
    def _handle_permission_feedback_toggled(self, event: TuiEvent) -> None:
        permission_id = str(event.payload.get("permission_id") or "").strip()
        if self.permission is None or self.permission.permission_id != permission_id:
            self.record_diagnostic("PERMISSION_FEEDBACK_MISMATCH", event)
            return
        selected_index = int(event.payload.get("selected_index") or 0)
        if selected_index != self.permission.selected_index:
            self.record_diagnostic("PERMISSION_FEEDBACK_SELECTION_MISMATCH", event)
            return
        self.permission = replace(
            self.permission,
            feedback_mode=bool(event.payload.get("feedback_mode")),
            feedback=str(event.payload.get("feedback") or ""),
            feedback_placeholder=str(event.payload.get("feedback_placeholder") or ""),
        )

    # LLM: feedback 文本只是给后续模型的上下文；更新时必须命中当前 permission/selection，不能解释为批准。
    # 函数用途: 将权限补充说明输入投影到当前面板。
    def _handle_permission_feedback_changed(self, event: TuiEvent) -> None:
        permission_id = str(event.payload.get("permission_id") or "").strip()
        if self.permission is None or self.permission.permission_id != permission_id:
            self.record_diagnostic("PERMISSION_FEEDBACK_MISMATCH", event)
            return
        selected_index = int(event.payload.get("selected_index") or 0)
        if selected_index != self.permission.selected_index:
            self.record_diagnostic("PERMISSION_FEEDBACK_SELECTION_MISMATCH", event)
            return
        self.permission = replace(
            self.permission,
            feedback=str(event.payload.get("feedback") or ""),
        )

    # LLM: resolved 只能关闭同 permission_id 的 overlay；冲突决定不能误关另一个请求。
    # 函数用途: 清除已完成的权限面板，并把活动工具恢复为 started 或 interrupted。
    def _handle_permission_resolved(self, event: TuiEvent) -> None:
        permission_id = str(event.payload.get("permission_id") or "").strip()
        if self.permission is None or self.permission.permission_id != permission_id:
            self.record_diagnostic("PERMISSION_RESOLUTION_MISMATCH", event)
            return
        decision = str(event.payload.get("decision") or "").strip().lower()
        self.permission = None
        block = self.active_blocks.get(event.block_id)
        if block is not None:
            next_phase = (
                "interrupted"
                if decision in {"deny", "denied", "cancel", "cancelled"}
                else "started"
            )
            self.active_blocks[event.block_id] = replace(
                block,
                phase=next_phase,
                updated_seq=event.seq,
            )

    # LLM: mixin 通过唯一 reducer 的有界诊断入口记录冲突；子类必须提供同签名实现。
    # 函数用途: 声明权限处理器依赖的诊断接口，实际由 TuiViewModelReducer 实现。
    def record_diagnostic(self, code: str, event: TuiEvent) -> None:
        raise NotImplementedError


# LLM: TuiViewModelReducer 是事件到显示状态的唯一转换表；权限方法按领域拆入内部 mixin，新增 kind 仍须注册 handler 并补状态转换测试。
# 类用途: 顺序应用已通过 journal 的事件，并维持 active→stable 一次冻结。
class TuiViewModelReducer(_TuiPermissionReducerMixin):
    # LLM: handler registry 明确列出已理解 kind，未知 kind 只记诊断，不把 payload 当文本透传。
    # 函数用途: 初始化空 view model、确认后的模型显示名、独立的刷新健康标记和事件处理表。
    def __init__(
        self,
        *,
        max_diagnostics: int = 200,
        max_stable_blocks: int = 20_000,
    ) -> None:
        self.stable_blocks: list[TuiBlock] = []
        self.active_blocks: dict[str, TuiBlock] = {}
        self.pending_steers: list[TuiPendingSteer] = []
        self.queued_inputs: list[TuiQueuedInput] = []
        self.permission: TuiPermissionOverlay | None = None
        self.status = TuiStatus()
        self.background_sync_failed = False
        self.selected_model_name = ""
        self.diagnostics: list[TuiDiagnostic] = []
        self.max_diagnostics = max(1, int(max_diagnostics or 1))
        self.max_stable_blocks = max(1, int(max_stable_blocks or 1))
        self._stable_ids: set[str] = set()
        self._task_progress_generation_id = ""
        self._task_progress_plan_revision = 0
        self._handlers: dict[str, Callable[[TuiEvent], None]] = {
            "session_started": self._handle_session_started,
            "session_model_selected": self._handle_session_model_selected,
            "connection_started": self._handle_connection_started,
            "connection_resolved": self._handle_connection_resolved,
            "background_sync_changed": self._handle_background_sync_changed,
            "background_activity_started": self._handle_background_activity_started,
            "background_activity_updated": self._handle_background_activity_updated,
            "background_activity_completed": self._handle_background_activity_completed,
            "task_progress_snapshot": self._handle_task_progress_snapshot,
            "task_progress_generation_started": (
                self._handle_task_progress_generation_started
            ),
            "user_message": self._handle_user_message,
            "system_message": self._handle_system_message,
            "interrupt_notice": self._handle_system_message,
            "compact_boundary": self._handle_compact_boundary,
            "context_window_compacted": self._handle_system_message,
            "conversation_compaction_started": self._handle_compact_started,
            "conversation_compaction_progress": self._handle_compact_progress,
            "conversation_compaction_completed": self._handle_compact_terminal,
            "conversation_compaction_superseded": self._handle_compact_terminal,
            "conversation_compaction_failed": self._handle_compact_terminal,
            "assistant_started": self._handle_block_started,
            "assistant_delta": self._handle_block_delta,
            "assistant_completed": self._handle_block_completed,
            "assistant_discarded": self._handle_block_discarded,
            "transcript_stream_closed": self._handle_transcript_stream_closed,
            "thinking_started": self._handle_block_started,
            "thinking_delta": self._handle_block_delta,
            "thinking_completed": self._handle_block_completed,
            "thinking_discarded": self._handle_block_discarded,
            "tool_input_started": self._handle_tool_input_started,
            "tool_input_progress": self._handle_tool_input_progress,
            "tool_input_completed": self._handle_tool_input_completed,
            "tool_started": self._handle_tool_started,
            "tool_progress": self._handle_tool_progress,
            "tool_completed": self._handle_tool_terminal,
            "tool_failed": self._handle_tool_terminal,
            "permission_requested": self._handle_permission_requested,
            "permission_selection_changed": self._handle_permission_selection_changed,
            "permission_feedback_toggled": self._handle_permission_feedback_toggled,
            "permission_feedback_changed": self._handle_permission_feedback_changed,
            "permission_resolved": self._handle_permission_resolved,
            "steer_added": self._handle_steer_added,
            "steer_removed": self._handle_steer_removed,
            "steer_promoted": self._handle_steer_promoted,
            "queue_added": self._handle_queue_added,
            "queue_removed": self._handle_queue_removed,
            "queue_restored": self._handle_queue_removed,
            "queue_promoted": self._handle_queue_promoted,
            "turn_started": self._handle_turn_status,
            "turn_interrupt_requested": self._handle_turn_status,
            "turn_completed": self._handle_turn_status,
            "turn_failed": self._handle_turn_status,
            "turn_interrupted": self._handle_turn_status,
            "status_updated": self._handle_status_updated,
            "history_blocks_reordered": self._handle_history_blocks_reordered,
            "history_page_prepended": self._handle_history_page_prepended,
        }

    # LLM: 按 canonical 块 ID 重排并沿用原 created_seq 显示槽位，防止 renderer 抵消顺序；不改 journal、内容或活动。
    # 函数用途: 实时客户端漏收较早过程后，补全快照时将同一工作片重新放回原顺序，而不是追加在 final 后。
    def _handle_history_blocks_reordered(self, event: TuiEvent) -> None:
        ids = event.payload.get("block_ids")
        final_id = event.payload.get("final_block_id")
        if (
            not event.request_id.startswith("bg-main:") or not isinstance(ids, list)
            or not isinstance(final_id, str) or not final_id.startswith("history:")
            or not ids or ids[-1] != final_id
            or not all(isinstance(key, str) and (key == final_id or key.startswith(f"{event.request_id}:")) for key in ids)
            or len(set(ids)) != len(ids)
        ):
            return
        id_set = set(ids)
        selected = {block.block_id: block for block in self.stable_blocks if block.block_id in id_set}
        if len(selected) != len(ids):
            return
        ordered = iter(selected[key] for key in ids)
        self.stable_blocks = [
            replace(next(ordered), created_seq=block.created_seq) if block.block_id in selected else block
            for block in self.stable_blocks
        ]

    # LLM: created_seq 在显示层充当排序槽位；仅更早页取得前置槽位，原事件 journal/运行时间/活动块不改。
    # 函数用途: 把后读到的历史放回正文前部，保留欢迎卡在顶部，不让 renderer 又按到达时间排回底部。
    def _handle_history_page_prepended(self, event: TuiEvent) -> None:
        ids = event.payload.get("block_ids")
        if not isinstance(ids, list) or not ids or not all(
            isinstance(key, str) and key.startswith(("history:", "bg-main:")) for key in ids
        ):
            return
        selected = {block.block_id: block for block in self.stable_blocks if block.block_id in ids}
        if len(selected) != len(ids):
            return
        remaining = [block for block in self.stable_blocks if block.block_id not in selected]
        start = min((block.created_seq for block in remaining), default=0) - len(ids) - 1
        prefix = [replace(selected[key], created_seq=start + index) for index, key in enumerate(ids)]
        self.stable_blocks = [
            replace(block, created_seq=start - 1) if block.kind == "session_started" else block
            for block in [*prefix, *remaining]
        ]

    # LLM: apply 只接受 journal 已裁决事件；异常 handler 转为诊断并保留上一合法快照。
    # 函数用途: 将一条 typed event 应用到 view model。
    def apply(self, event: TuiEvent) -> None:
        handler = self._handlers.get(event.kind)
        if handler is None:
            self.record_diagnostic("UNKNOWN_EVENT_KIND", event)
            return
        try:
            handler(event)
            self._touch_active_status(event)
        except (KeyError, TypeError, ValueError):
            self.record_diagnostic("INVALID_EVENT_PAYLOAD", event)

    # LLM: 活动时间由已识别任务事件推进；客户端刷新健康不能冒充模型进展或重置 stall 计时。
    # 函数用途: 运行回合收到新任务事件后刷新活动时间，忽略状态连接失败/恢复通知。
    def _touch_active_status(self, event: TuiEvent) -> None:
        if (
            event.kind != "background_sync_changed"
            and self.status.phase in {"running", "interrupting"}
            and event.created_at > self.status.last_event_at
        ):
            self.status = replace(self.status, last_event_at=event.created_at)

    # LLM: diagnostics 只保存身份和代码，不复制可能含敏感内容的 payload。
    # 函数用途: 追加一条有界 reducer/journal 诊断。
    def record_diagnostic(self, code: str, event: TuiEvent) -> None:
        self.diagnostics.append(
            TuiDiagnostic(str(code), event.event_id, event.kind, event.block_id)
        )
        if len(self.diagnostics) > self.max_diagnostics:
            del self.diagnostics[: len(self.diagnostics) - self.max_diagnostics]

    # LLM: snapshot 按 stable 顺序和 active 插入顺序复制，不暴露内部可变容器。
    # 函数用途: 返回当前不可变渲染快照，同时保留与任务运行状态分离的刷新健康。
    def snapshot(self) -> TuiViewSnapshot:
        return TuiViewSnapshot(
            stable_blocks=tuple(self.stable_blocks),
            active_blocks=tuple(self.active_blocks.values()),
            pending_steers=tuple(self.pending_steers),
            queued_inputs=tuple(self.queued_inputs),
            permission=self.permission,
            status=self.status,
            diagnostics=tuple(self.diagnostics),
            background_sync_failed=self.background_sync_failed,
            selected_model_name=self.selected_model_name,
        )

    # LLM: 刷新结果必须是 typed bool，只改变本客户端显示健康；不得清空旧快照、终结任务或清除待发消息。
    # 函数用途: 标记后台状态是否读取失败，成功后收起提示；不把一次网络故障判断为代理失败。
    def _handle_background_sync_changed(self, event: TuiEvent) -> None:
        ok = event.payload.get("ok")
        if not isinstance(ok, bool):
            raise ValueError("background sync requires boolean ok")
        self.background_sync_failed = not ok

    # LLM: session_started 只生成一次欢迎块；初始模型名随后可被已确认的选择事件更新，不修改历史正文。
    # 函数用途: 建立欢迎块和初始模型显示名。
    def _handle_session_started(self, event: TuiEvent) -> None:
        block = self._block_from_event(event, role="system", phase="completed")
        self._append_stable(block, event)
        if not self.selected_model_name:
            self.selected_model_name = str(event.payload.get("model") or "")

    # LLM: 仅投影宿主确认的公开模型名，不重建欢迎块、切换后端或改变 active turn。
    # 函数用途: 让顶部欢迎区随模型选择刷新，保留已有历史和当前执行。
    def _handle_session_model_selected(self, event: TuiEvent) -> None:
        model = event.payload.get("model")
        if isinstance(model, str) and model.strip():
            self.selected_model_name = model.strip()

    # LLM: connectivity 是真实 Gateway readiness 的临时显示块；同 id 重复启动不能覆盖既有活动或稳定消息。
    # 函数用途: 在欢迎区下方显示正在连接 Gateway 的原位 spinner。
    def _handle_connection_started(self, event: TuiEvent) -> None:
        if event.block_id in self._stable_ids or event.block_id in self.active_blocks:
            self.record_diagnostic("CONNECTION_RESTART_REJECTED", event)
            return
        self.active_blocks[event.block_id] = self._block_from_event(
            event,
            role="connection",
            phase="started",
        )

    # LLM: readiness 成功只移除临时块，失败才冻结 typed error；不能把一次成功连接伪装成会话消息。
    # 函数用途: 收起连接 spinner，或在 Gateway 超时后留下明确错误。
    def _handle_connection_resolved(self, event: TuiEvent) -> None:
        active = self.active_blocks.pop(event.block_id, None)
        if active is None:
            self.record_diagnostic("CONNECTION_WITHOUT_START", event)
            return
        if event.payload.get("ok") is True:
            return
        failed = replace(
            active,
            kind=event.kind,
            role="error",
            phase="failed",
            text=str(event.payload.get("text") or "Gateway is unavailable."),
            updated_seq=event.seq,
            metadata={**active.metadata, **_public_metadata(event.payload)},
        )
        self._append_stable(failed, event)

    # LLM: Background activity is a removable display projection, not a turn or
    # transcript message. Its lifecycle is keyed by one stable session block.
    # 函数用途: 建立前台让出后仍有会话任务运行的常驻 Working 块。
    def _handle_background_activity_started(self, event: TuiEvent) -> None:
        if event.block_id in self._stable_ids or event.block_id in self.active_blocks:
            self.record_diagnostic("BACKGROUND_ACTIVITY_RESTART_REJECTED", event)
            return
        self.active_blocks[event.block_id] = self._block_from_event(
            event,
            role="background",
            phase="started",
        )
        self.status = _status_with_update(self.status, event)
        self._consume_task_progress_items(event)

    # LLM: Count updates may change only the existing projection; they cannot
    # synthesize activity after a missed start event.
    # 函数用途: 更新后台 Working 块显示的真实进行中任务数量。
    def _handle_background_activity_updated(self, event: TuiEvent) -> None:
        block = self.active_blocks.get(event.block_id)
        if block is None:
            self.record_diagnostic("BACKGROUND_ACTIVITY_UPDATE_WITHOUT_START", event)
            return
        self.active_blocks[event.block_id] = replace(
            block,
            phase="updated",
            updated_seq=event.seq,
            metadata={**block.metadata, **_public_metadata(event.payload)},
        )
        self.status = _status_with_update(self.status, event)
        self._consume_task_progress_items(event)

    # LLM: A zero active-task count removes the projection without freezing a fake chat message,
    # but the same canonical snapshot may hydrate compact/context status while no block exists.
    # 函数用途: 会话没有进行中任务时收起 Working，同时保留同帧携带的权威 Compact 与 Context 状态。
    def _handle_background_activity_completed(self, event: TuiEvent) -> None:
        self.active_blocks.pop(event.block_id, None)
        self.status = _status_with_update(self.status, event)
        self._consume_task_progress_items(event)

    # LLM: A final canonical Todo snapshot may arrive after Working was removed.
    # It updates only the existing display ledger and cannot reopen a task or turn.
    # 函数用途: 用后台最终消息携带的结构化进度刷新清单勾选状态。
    def _handle_task_progress_snapshot(self, event: TuiEvent) -> None:
        self._consume_task_progress_items(event)

    # LLM: Only an explicit dequeued-turn event may advance the expected Todo
    # generation. This is presentation state: clearing the block cannot alter the
    # durable ledger, child status, completion, or scheduling.
    # 函数用途: 开始新用户回合时收起上一轮 Todo，并拒绝随后迟到的旧快照。
    def _handle_task_progress_generation_started(self, event: TuiEvent) -> None:
        generation_id = str(
            event.payload.get("task_progress_generation_id") or ""
        ).strip()
        if not generation_id:
            raise ValueError("task_progress_generation_id required")
        self._task_progress_generation_id = generation_id
        self._task_progress_plan_revision = 0
        self.active_blocks.pop("todo:task_progress", None)

    # LLM: 用户消息始终直接进入稳定历史，不能留在 active 后被模型终态覆盖。
    # 函数用途: 追加一个用户输入块。
    def _handle_user_message(self, event: TuiEvent) -> None:
        self._append_stable(
            self._block_from_event(event, role="user", phase="completed"),
            event,
        )

    # LLM: 系统/错误消息的 severity 只影响 metadata/style，不能改变 turn 或工具状态。
    # 函数用途: 追加一个稳定系统消息块。
    def _handle_system_message(self, event: TuiEvent) -> None:
        role = "error" if str(event.payload.get("severity") or "") == "error" else "system"
        self._append_stable(
            self._block_from_event(event, role=role, phase=event.phase),
            event,
        )

    # LLM: Only a canonical compact boundary may advance the visible main-agent count. The last
    # provider preflight snapshot describes the pre-compact history, so it must be retired until
    # the next real model call publishes a fresh model_visible_context_usage.v1 snapshot.
    # 函数用途: 记录成功 Compact 次数、清掉已失效的旧 Context 数字，并保留稳定边界提示。
    def _handle_compact_boundary(self, event: TuiEvent) -> None:
        generation = _nonnegative_int(event.payload.get("compact_generation"), 0)
        if generation <= 0:
            raise ValueError("compact_generation must be positive")
        self.status = replace(
            self.status,
            compact_count=max(self.status.compact_count, generation),
            context_tokens=0,
            context_usage=None,
        )
        self._handle_system_message(event)

    # LLM: Compact 开始事件必须创建独立 active block，不得复用 thinking 或修改会话 compact generation。
    # 函数用途: 建立一个原位更新的会话 Compact 进度块。
    def _handle_compact_started(self, event: TuiEvent) -> None:
        if event.block_id in self._stable_ids or event.block_id in self.active_blocks:
            self.record_diagnostic("COMPACT_RESTART_REJECTED", event)
            return
        self.active_blocks[event.block_id] = self._block_from_event(
            event,
            role="compact",
            phase="started",
        )

    # LLM: Compact progress 只能更新同 id 活动块，百分比单调不退但 stage 仍保留底层当前事实。
    # 函数用途: 刷新 Compact 阶段、百分比与压缩前后 token 计数。
    def _handle_compact_progress(self, event: TuiEvent) -> None:
        block = self.active_blocks.get(event.block_id)
        if block is None:
            self.record_diagnostic("COMPACT_PROGRESS_WITHOUT_START", event)
            return
        incoming = _public_metadata(event.payload)
        incoming["percent"] = max(
            int(block.metadata.get("percent") or 0),
            int(incoming.get("percent") or 0),
        )
        self.active_blocks[event.block_id] = replace(
            block,
            phase="updated",
            updated_seq=event.seq,
            metadata={**block.metadata, **incoming},
        )

    # LLM: Completed is replaced by canonical compact_boundary; superseded means no candidate
    # committed and is removed silently. Only a real failure/interruption freezes evidence.
    # 函数用途: 收起已完成或未采用的 Compact 进度条，只为真实失败保留红色提示。
    def _handle_compact_terminal(self, event: TuiEvent) -> None:
        active = self.active_blocks.pop(event.block_id, None)
        if active is None:
            self.record_diagnostic("COMPACT_TERMINAL_WITHOUT_START", event)
            return
        if event.kind in {
            "conversation_compaction_completed",
            "conversation_compaction_superseded",
        }:
            return
        failed = replace(
            active,
            phase=event.phase if event.phase in TERMINAL_BLOCK_PHASES else "failed",
            updated_seq=event.seq,
            metadata={**active.metadata, **_public_metadata(event.payload)},
        )
        self._append_stable(failed, event)

    # LLM: started 对已经 stable/active 的 block 都视为非法重启，避免终态回退或双活动块。
    # 函数用途: 创建 assistant/thinking 活动块。
    def _handle_block_started(self, event: TuiEvent) -> None:
        if event.block_id in self._stable_ids or event.block_id in self.active_blocks:
            self.record_diagnostic("BLOCK_RESTART_REJECTED", event)
            return
        self.active_blocks[event.block_id] = self._block_from_event(
            event,
            role=_role_for_kind(event.kind),
            phase="started",
        )

    # LLM: delta 必须命中现有 active block，不能凭迟到正文自动创建第二块。
    # 函数用途: 将 text/detail 增量合并到活动块。
    def _handle_block_delta(self, event: TuiEvent) -> None:
        block = self.active_blocks.get(event.block_id)
        if block is None:
            self.record_diagnostic("DELTA_WITHOUT_ACTIVE_BLOCK", event)
            return
        text = block.text + str(event.payload.get("text") or "")
        detail = block.detail + str(event.payload.get("detail") or "")
        self.active_blocks[event.block_id] = replace(
            block,
            phase="delta",
            text=text,
            detail=detail,
            updated_seq=event.seq,
        )

    # LLM: canonical final 可接替已验证的同片候选 ID，不能删除稳定历史或其它角色；重放无副作用。
    # 函数用途: 将完整正文原子替换活动半句并固定为历史，避免先删再添造成闪烁或两份回复。
    def _handle_block_completed(self, event: TuiEvent) -> None:
        old_id = event.payload.get("replaces_live_block_id")
        if (
            event.kind == "assistant_completed" and event.request_id.startswith("history:")
            and isinstance(old_id, str) and old_id.startswith("bg-main:")
            and event.block_id not in self._stable_ids
        ):
            candidate = self.active_blocks.get(old_id)
            if candidate is not None and candidate.role == "assistant":
                self.active_blocks.pop(old_id)
                self.active_blocks[event.block_id] = replace(candidate, block_id=event.block_id, kind=event.kind)
        self._freeze_block(event, role=_role_for_kind(event.kind))

    # LLM: 只清 exact display request 的活动块，不关闭真实 turn/权限；未知工具/Compact 保留中性缺口提示。
    # 函数用途: 前台取消或异常且没有 final 时收起孤立动画，不能将未观察到的执行结果标成成功。
    def _handle_transcript_stream_closed(self, event: TuiEvent) -> None:
        if not event.request_id.startswith("bg-main:"):
            return
        for key, block in tuple(self.active_blocks.items()):
            if not key.startswith(f"{event.request_id}:") or block.role not in {"assistant", "thinking", "tool_input", "tool", "compact"}:
                continue
            self.active_blocks.pop(key)
            if block.role in {"tool", "compact"}:
                self._append_stable(replace(
                    block, role="system", kind="system_message", phase="completed",
                    text=f"{block.title or '过程块'}：本工作片未保存完整结果。", detail="", updated_seq=event.seq,
                ), event)

    # LLM: discarded 仅删除同 identity 的易失块，不能删除稳定历史或把未知 block 当作成功；仅 typed 边界可调用。
    # 函数用途: 移除空思考动画或插话前未确认的候选半句，不抹掉已提交的回复和思考。
    def _handle_block_discarded(self, event: TuiEvent) -> None:
        if event.block_id in self._stable_ids:
            self.record_diagnostic("DISCARD_STABLE_BLOCK_REJECTED", event)
            return
        if self.active_blocks.pop(event.block_id, None) is None:
            self.record_diagnostic("DISCARD_WITHOUT_ACTIVE_BLOCK", event)

    # LLM: tool_started 只使用结构化 tool/title/detail 字段，并先清同轮易失
    # 参数行；不解析 legacy 文本，也不把参数 ready 当工具开始。
    # 函数用途: 收起参数生成提示并创建一个运行中的工具卡片。
    def _handle_tool_started(self, event: TuiEvent) -> None:
        self._clear_tool_input_blocks()
        if event.block_id in self._stable_ids or event.block_id in self.active_blocks:
            self.record_diagnostic("BLOCK_RESTART_REJECTED", event)
            return
        self.active_blocks[event.block_id] = self._block_from_event(
            event,
            role="tool",
            phase="started",
        )

    # LLM: 该 block 只显示 provider 参数生成活性，不能复用 tool role 或进入
    # stable 历史；payload 已在共享合同处移除所有参数正文。
    # 函数用途: 创建一条可动画、可原位删除的“正在准备工具参数”临时行。
    def _handle_tool_input_started(self, event: TuiEvent) -> None:
        if event.block_id in self._stable_ids or event.block_id in self.active_blocks:
            self.record_diagnostic("BLOCK_RESTART_REJECTED", event)
            return
        self.active_blocks[event.block_id] = self._block_from_event(
            event,
            role="tool_input",
            phase="started",
        )

    # LLM: 进度更新只能覆盖累计字符计数和工具名等公开元数据；它既不
    # 冻结 block，也不能推进真实工具或回合状态。
    # 函数用途: 原位刷新工具参数生成量，避免长写入期间看起来卡死。
    def _handle_tool_input_progress(self, event: TuiEvent) -> None:
        block = self.active_blocks.get(event.block_id)
        if block is None or block.role != "tool_input":
            self.record_diagnostic("TOOL_INPUT_PROGRESS_WITHOUT_START", event)
            return
        self.active_blocks[event.block_id] = replace(
            block,
            phase="updated",
            title=str(event.payload.get("tool") or block.title),
            updated_seq=event.seq,
            metadata={**block.metadata, **_public_metadata(event.payload)},
        )

    # LLM: ready/clear 仅删除易失进度块，绝不追加一条“工具已完成”的稳定
    # 历史；迟到事件允许幂等忽略，避免重试/终态竞态制造诊断噪声。
    # 函数用途: 参数闭合、重试或回合结束时收起临时行。
    def _handle_tool_input_completed(self, event: TuiEvent) -> None:
        block = self.active_blocks.get(event.block_id)
        if block is not None and block.role == "tool_input":
            self.active_blocks.pop(event.block_id, None)

    # LLM: 清理范围严格限定 role=tool_input；真实 tool/thinking/assistant
    # active block 不能被一次 provider 参数边界误删。
    # 函数用途: 在真实工具开始或回合终态时兜底清除所有参数临时行。
    def _clear_tool_input_blocks(self) -> None:
        for block_id in tuple(
            block_id
            for block_id, block in self.active_blocks.items()
            if block.role == "tool_input"
        ):
            self.active_blocks.pop(block_id, None)

    # LLM: tool_progress 只能更新同一 active tool id；output/detail 都由上游脱敏/有界投影提供。
    # 函数用途: 更新工具卡的阶段、摘要和结果预览。
    def _handle_tool_progress(self, event: TuiEvent) -> None:
        block = self.active_blocks.get(event.block_id)
        if block is None:
            self.record_diagnostic("TOOL_PROGRESS_WITHOUT_START", event)
            return
        detail = str(event.payload.get("detail") or event.payload.get("output") or block.detail)
        self.active_blocks[event.block_id] = replace(
            block,
            phase=str(event.payload.get("phase") or event.phase),
            detail=detail,
            updated_seq=event.seq,
            metadata={**block.metadata, **_public_metadata(event.payload)},
        )
        # task_progress 工具: 更新 todo 面板块(□/☑/● 自动打钩)
        self._consume_task_progress_items(event)

    # LLM: task_progress_items is a replace-all snapshot within one exact opaque
    # generation. A mismatched/older projection is ignored, absent data preserves
    # the prior block, and an explicit matching empty list removes it.
    # 函数用途: 用当前用户回合的结构化快照替换 Todo，并拦住上一轮迟到数据。
    def _consume_task_progress_items(self, event: TuiEvent) -> None:
        items = event.payload.get("task_progress_items")
        if not isinstance(items, list):
            return
        incoming_generation = str(
            event.payload.get("task_progress_generation_id") or ""
        ).strip()
        expected_generation = self._task_progress_generation_id
        if expected_generation and incoming_generation != expected_generation:
            return
        if incoming_generation and not expected_generation:
            self._task_progress_generation_id = incoming_generation
        incoming_revision = _nonnegative_int(
            event.payload.get("task_progress_plan_revision"),
            0,
        )
        if (
            incoming_generation
            and incoming_generation == self._task_progress_generation_id
            and incoming_revision < self._task_progress_plan_revision
        ):
            return
        if incoming_generation:
            self._task_progress_plan_revision = max(
                self._task_progress_plan_revision,
                incoming_revision,
            )
        if not items:
            self.active_blocks.pop("todo:task_progress", None)
            return
        self._update_todo_block(
            items,
            event.seq,
            generation_id=incoming_generation,
            plan_revision=incoming_revision,
        )

    # LLM: todo 面板是 task_progress 账本的持续投影; 每次 items 更新替换整块
    # (block 内容即快照, 不增量合并, 避免跨轮残留旧项)。
    # 函数用途: 更新 todo 面板块(role=todo)。
    def _update_todo_block(
        self,
        items: list[dict[str, Any]],
        seq: int,
        *,
        generation_id: str = "",
        plan_revision: int = 0,
    ) -> None:
        clean = [
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "status": str(item.get("status") or "pending"),
            }
            for item in items
            if isinstance(item, dict)
        ]
        block_id = "todo:task_progress"
        existing = self.active_blocks.get(block_id)
        metadata = {**existing.metadata} if existing is not None else {}
        metadata["items"] = clean
        metadata["updated_at"] = seq
        metadata["task_progress_generation_id"] = str(generation_id or "")
        metadata["task_progress_plan_revision"] = max(0, int(plan_revision or 0))
        self.active_blocks[block_id] = TuiBlock(
            block_id=block_id,
            kind="task_progress",
            role="todo",
            phase="active",
            text="",
            title="任务清单",
            created_seq=(existing.created_seq if existing is not None else seq),
            updated_seq=seq,
            metadata=metadata,
        )

    # LLM: tool terminal 按 ok/phase 冻结一次；permission_required 不是 terminal，必须留在 active。
    # 函数用途: 完成或失败一个工具卡片（task_progress 终态同时更新 todo 面板）。
    def _handle_tool_terminal(self, event: TuiEvent) -> None:
        self._consume_task_progress_items(event)
        self._freeze_block(event, role="tool")

    # LLM: Pending steer identity comes only from the client-generated opaque id. Duplicate ids
    # are replay diagnostics and text is never used for correlation.
    # 函数用途: 加入一条等待 Gateway 确认已注入当前任务的补充消息。
    def _handle_steer_added(self, event: TuiEvent) -> None:
        message_id = str(event.payload.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("steer message_id required")
        if any(item.message_id == message_id for item in self.pending_steers):
            self.record_diagnostic("STEER_ID_REPLAY", event)
            return
        self.pending_steers.append(
            TuiPendingSteer(
                message_id=message_id,
                text=str(event.payload.get("text") or ""),
                seq=event.seq,
            )
        )
        self.pending_steers.sort(key=lambda item: item.seq)

    # LLM: A rejected transport/control race removes only the exact pending id and cannot alter
    # the canonical next-turn queue.
    # 函数用途: Gateway 未接收活动回合补充时，撤下对应的等待提示。
    def _handle_steer_removed(self, event: TuiEvent) -> None:
        message_id = str(event.payload.get("message_id") or "").strip()
        before = len(self.pending_steers)
        self.pending_steers[:] = [
            item for item in self.pending_steers if item.message_id != message_id
        ]
        if len(self.pending_steers) == before:
            self.record_diagnostic("STEER_ID_NOT_FOUND", event)

    # LLM: Promotion requires an existing exact pending id and keeps the original local submit
    # sequence. A late Gateway receipt must place the user row before later output from the same
    # request instead of appending it after the already-frozen final.
    # 函数用途: 模型运行时确认收到补充消息后，按原提交位置把它从等待区移入对话历史。
    def _handle_steer_promoted(self, event: TuiEvent) -> None:
        message_id = str(event.payload.get("message_id") or "").strip()
        pending = next(
            (item for item in self.pending_steers if item.message_id == message_id),
            None,
        )
        if pending is None:
            self.record_diagnostic("STEER_ID_NOT_FOUND", event)
            return
        self.pending_steers[:] = [
            item for item in self.pending_steers if item.message_id != message_id
        ]
        promoted = replace(
            event,
            payload={**event.payload, "text": pending.text},
        )
        block = replace(
            self._block_from_event(promoted, role="user", phase="completed"),
            created_seq=pending.seq,
        )
        self._insert_promoted_user(block, promoted)

    # LLM: This insertion is intentionally scoped to one promoted user message and one request.
    # Global stable ordering remains completion-ordered, while a delayed transport receipt may
    # move only ahead of same-turn blocks that began after the user's real local submission.
    # 函数用途: 把迟到确认的插话插到同回合后续思考/工具/回答之前，不重排其他历史块。
    def _insert_promoted_user(self, block: TuiBlock, event: TuiEvent) -> None:
        if block.block_id in self._stable_ids:
            self.record_diagnostic("STABLE_BLOCK_REPLAY", event)
            return
        request_id = str(event.request_id or "").strip()
        insert_at = len(self.stable_blocks)
        if request_id:
            request_prefixes = tuple(
                f"{kind}:{request_id}:"
                for kind in ("assistant", "thinking", "tool", "tool-input", "compact")
            )
            for index, existing in enumerate(self.stable_blocks):
                if (
                    existing.block_id.startswith(request_prefixes)
                    and existing.created_seq > block.created_seq
                ):
                    insert_at = index
                    break
        self._stable_ids.add(block.block_id)
        self.stable_blocks.insert(insert_at, block)
        overflow = len(self.stable_blocks) - self.max_stable_blocks
        if overflow > 0:
            del self.stable_blocks[:overflow]

    # LLM: queue item 身份必须来自 canonical queue id；重复 id 不可追加第二份输入。
    # 函数用途: 按优先级和 seq 加入一条排队输入。
    def _handle_queue_added(self, event: TuiEvent) -> None:
        queue_id = str(event.payload.get("queue_id") or event.block_id).strip()
        if any(item.queue_id == queue_id for item in self.queued_inputs):
            self.record_diagnostic("QUEUE_ID_REPLAY", event)
            return
        self.queued_inputs.append(
            TuiQueuedInput(
                queue_id=queue_id,
                text=str(event.payload.get("text") or ""),
                priority=str(event.payload.get("priority") or "next"),
                seq=event.seq,
            )
        )
        self.queued_inputs.sort(key=_queue_sort_key)

    # LLM: remove/restored 都按 queue_id 精确删除，恢复到编辑器由 input controller 自己处理 typed intent。
    # 函数用途: 从显示队列移除一个项目。
    def _handle_queue_removed(self, event: TuiEvent) -> None:
        queue_id = str(event.payload.get("queue_id") or event.block_id).strip()
        before = len(self.queued_inputs)
        self.queued_inputs[:] = [item for item in self.queued_inputs if item.queue_id != queue_id]
        if len(self.queued_inputs) == before:
            self.record_diagnostic("QUEUE_ID_NOT_FOUND", event)

    # LLM: promote 是 queue preview→stable user block 的单事件转换；它必须先核对 queue_id，不能留下重复预览或凭正文创建任务。
    # 函数用途: 在 worker 真正开始消费排队任务时，将同一输入原子移入稳定对话历史。
    def _handle_queue_promoted(self, event: TuiEvent) -> None:
        queue_id = str(event.payload.get("queue_id") or "").strip()
        if not queue_id or not any(item.queue_id == queue_id for item in self.queued_inputs):
            self.record_diagnostic("QUEUE_ID_NOT_FOUND", event)
            return
        self.queued_inputs[:] = [item for item in self.queued_inputs if item.queue_id != queue_id]
        self._append_stable(
            self._block_from_event(event, role="user", phase="completed"),
            event,
        )

    # LLM: turn 状态只由 kind 映射；终态先清易失参数行，payload 文案不参与
    # running/failed/interrupted 判断。
    # 函数用途: 收口临时展示并更新当前回合状态和事件时间。
    def _handle_turn_status(self, event: TuiEvent) -> None:
        if event.kind in {"turn_completed", "turn_failed", "turn_interrupted"}:
            self._clear_tool_input_blocks()
        self.status = _status_for_turn_event(self.status, event)

    # LLM: status_updated 只接受显式数值/模式字段，不能从格式化统计行反解析。
    # 函数用途: 合并 token、tool round、activity 和 mode 状态。
    def _handle_status_updated(self, event: TuiEvent) -> None:
        self.status = _status_with_update(self.status, event)

    # LLM: block 工厂只拷贝公开字段，禁止把完整未知 payload 塞进 metadata 后被 renderer 意外展示。
    # 函数用途: 从事件构造一个新显示块。
    def _block_from_event(self, event: TuiEvent, *, role: str, phase: str) -> TuiBlock:
        return _block_from_tui_event(event, role=role, phase=phase)

    # LLM: stable id 一旦存在后续事件都不能追加第二份或覆盖旧终态；显示窗口有界但 seen id 保留以拒绝迟到重放。
    # 函数用途: 幂等追加一个稳定块，并裁剪超出 TUI 投影上限的最旧块。
    def _append_stable(self, block: TuiBlock, event: TuiEvent) -> None:
        if block.block_id in self._stable_ids:
            self.record_diagnostic("STABLE_BLOCK_REPLAY", event)
            return
        self._stable_ids.add(block.block_id)
        self.stable_blocks.append(block)
        overflow = len(self.stable_blocks) - self.max_stable_blocks
        if overflow > 0:
            del self.stable_blocks[:overflow]

    # LLM: freeze 是 active→stable 的唯一出口；terminal-only 恢复必须携带显式完整 text/detail/title。
    # 函数用途: 完成活动块，或从带完整内容的 terminal event 恢复一个稳定块。
    def _freeze_block(self, event: TuiEvent, *, role: str) -> None:
        if event.block_id in self._stable_ids:
            self.record_diagnostic("TERMINAL_BLOCK_REPLAY", event)
            return
        active = self.active_blocks.pop(event.block_id, None)
        if active is None:
            if not _terminal_has_content(event.payload):
                self.record_diagnostic("TERMINAL_WITHOUT_BLOCK", event)
                return
            active = self._block_from_event(event, role=role, phase=event.phase)
        terminal_phase = event.phase if event.phase in TERMINAL_BLOCK_PHASES else "completed"
        text = str(event.payload.get("text") or active.text)
        detail = (
            _tool_terminal_detail(event, active)
            if role == "tool"
            else str(
                event.payload.get("detail")
                or event.payload.get("output")
                or active.detail
            )
        )
        title = str(event.payload.get("title") or event.payload.get("tool") or active.title)
        block = replace(
            active,
            phase=terminal_phase,
            text=text,
            detail=detail,
            title=title,
            updated_seq=event.seq,
            metadata={**active.metadata, **_public_metadata(event.payload)},
        )
        self._append_stable(block, event)


# LLM: TuiStateStore 将 journal 与 reducer 锁在同一原子 publish 内，并只在接受新事件后通知 UI。
# 类用途: 提供跨 worker/UI 线程安全的 publish、snapshot 和 redraw 订阅。
class TuiStateStore:
    # LLM: store 只拥有显示账和 reducer，不持有业务 agent/tool executor。
    # 函数用途: 创建可选自定义 journal/reducer 的状态容器。
    def __init__(
        self,
        journal: TuiEventJournal | None = None,
        reducer: TuiViewModelReducer | None = None,
    ) -> None:
        self.journal = journal or TuiEventJournal()
        self.reducer = reducer or TuiViewModelReducer()
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[], None]] = []

    # LLM: Journal/reducer mutation is canonical, while redraw subscribers are best-effort UI
    # notifications. A closed or broken renderer must never turn an already-applied business event
    # into a failed delivery receipt or make a durable control outbox replay forever.
    # 函数用途: 原子发布状态事件，再尽力通知界面刷新；单个重绘失败不会反咬业务状态。
    def publish(self, event: TuiEvent) -> JournalAppendResult:
        with self._lock:
            result = self.journal.append(event)
            if result.accepted:
                self.reducer.apply(event)
            elif result.status == "rejected":
                self.reducer.record_diagnostic(result.reason.upper(), event)
            subscribers = tuple(self._subscribers) if result.accepted else ()
        for callback in subscribers:
            try:
                callback()
            except Exception:  # noqa: BLE001 - redraw is explicitly weaker than canonical state
                _LOGGER.debug("TUI redraw subscriber failed", exc_info=True)
        return result

    # LLM: snapshot 在 reducer 锁域内复制，避免 UI 看见一半应用的事件。
    # 函数用途: 读取当前不可变 view snapshot。
    def snapshot(self) -> TuiViewSnapshot:
        with self._lock:
            return self.reducer.snapshot()

    # LLM: subscriber 只能请求 redraw，不得在 callback 中重入 publish 或执行业务动作。
    # 函数用途: 注册状态更新后的轻量通知函数。
    def subscribe(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    # LLM: invalidate 仅通知纯显示选项/动画变化，不写 journal 或 reducer；调用方不得用它隐藏业务状态变更。
    # 函数用途: 请求所有已注册界面重新渲染当前快照。
    def invalidate(self) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            callback()


# LLM: 角色只按事件 kind 映射，不读 payload 正文。
# 函数用途: 返回 assistant/thinking block 的显示角色。
def _role_for_kind(kind: str) -> str:
    return "thinking" if str(kind).startswith("thinking_") else "assistant"


# LLM: block 工厂只拷贝公开字段，禁止把完整未知 payload 塞进 metadata 后被 renderer 意外展示。
# 函数用途: 从事件构造一个新的显示块。
def _block_from_tui_event(event: TuiEvent, *, role: str, phase: str) -> TuiBlock:
    return TuiBlock(
        block_id=event.block_id,
        kind=event.kind,
        role=role,
        phase=phase,
        text=str(event.payload.get("text") or ""),
        title=str(event.payload.get("title") or event.payload.get("tool") or ""),
        detail=str(event.payload.get("detail") or event.payload.get("output") or ""),
        created_seq=event.seq,
        updated_seq=event.seq,
        metadata=_public_metadata(event.payload),
    )


# LLM: turn 状态映射仅依赖 typed kind 和显式时间/活动字段，不解析任何展示文案。
# 函数用途: 根据一次回合生命周期事件返回新的状态快照。
def _status_for_turn_event(status: TuiStatus, event: TuiEvent) -> TuiStatus:
    phase = {
        "turn_started": "running",
        "turn_interrupt_requested": "interrupting",
        "turn_completed": "idle",
        "turn_failed": "failed",
        "turn_interrupted": "interrupted",
    }[event.kind]
    started_at = event.created_at if event.kind == "turn_started" else status.started_at
    return replace(
        status,
        phase=phase,
        activity=str(event.payload.get("activity") or status.activity),
        started_at=started_at,
        last_event_at=event.created_at,
    )


# LLM: 数值状态合并只接受显式 payload 字段并保留无效值前的合法事实；context_usage 仅接受冻结 schema 的数字白名单。
# 函数用途: 合并 token、工具轮次、实时上下文、活动和显示模式。
def _status_with_update(status: TuiStatus, event: TuiEvent) -> TuiStatus:
    context_usage = _context_usage_from_mapping(
        event.payload.get("context_usage"),
        status.context_usage,
    )
    context_tokens = _nonnegative_int(
        event.payload.get("context_tokens"),
        status.context_tokens,
    )
    raw_context_usage = event.payload.get("context_usage")
    if (
        context_usage is not None
        and isinstance(raw_context_usage, Mapping)
        and raw_context_usage.get("schema") == "model_visible_context_usage.v1"
    ):
        context_tokens = context_usage.current_tokens
    return replace(
        status,
        activity=str(event.payload.get("activity") or status.activity),
        last_event_at=event.created_at or status.last_event_at,
        context_tokens=context_tokens,
        output_tokens=_nonnegative_int(event.payload.get("output_tokens"), status.output_tokens),
        tool_rounds=_nonnegative_int(event.payload.get("tool_rounds"), status.tool_rounds),
        compact_count=max(
            status.compact_count,
            _nonnegative_int(
                event.payload.get("compact_count"),
                status.compact_count,
            ),
        ),
        mode=str(event.payload.get("mode") or status.mode),
        context_usage=context_usage,
    )


# LLM: The reducer validates the context schema locally even after Gateway sanitization so
# replayed, duplicated, or direct local events cannot inject unknown display state.
# 函数用途: 将合法的实时上下文事件转换成不可变 TUI 快照，畸形事件保留上一份数据。
def _context_usage_from_mapping(
    value: object,
    previous: TuiContextUsage | None,
) -> TuiContextUsage | None:
    if not isinstance(value, Mapping):
        return previous
    if value.get("schema") != "model_visible_context_usage.v1":
        return previous
    protocol = str(value.get("protocol") or "unknown")
    return TuiContextUsage(
        current_tokens=_nonnegative_int(value.get("current_tokens"), 0),
        context_window_tokens=_nonnegative_int(value.get("context_window_tokens"), 0),
        compact_trigger_tokens=_nonnegative_int(value.get("compact_trigger_tokens"), 0),
        prompt_tokens=_nonnegative_int(value.get("prompt_tokens"), 0),
        messages_tokens=_nonnegative_int(value.get("messages_tokens"), 0),
        runtime_guidance_tokens=_nonnegative_int(value.get("runtime_guidance_tokens"), 0),
        tool_schema_tokens=_nonnegative_int(value.get("tool_schema_tokens"), 0),
        estimated=value.get("estimated") is True,
        protocol=protocol if protocol in {"native", "text"} else "unknown",
    )


# LLM: metadata 白名单防止未知 payload 或秘密被 renderer/debug 无意展示。
# 函数用途: 选择允许进入显示块的结构化元数据。
def _public_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "severity",
        "tool",
        "round",
        "call_index",
        "ok",
        "error_code",
        "failure_stage",
        "duration_ms",
        "handler_executed",
        "display",
        "invocation",
        "duration_seconds",
        "started_at",
        "request_id",
        "model",
        "version",
        "workspace",
        "compact_generation",
        "generation",
        "operation_id",
        "source_kind",
        "commit_authority",
        "before_tokens",
        "after_tokens",
        "trigger_tokens",
        "dropped_pairs",
        "preserved_pairs",
        "stage",
        "percent",
        "indeterminate",
        "source_messages",
        "process",
        "active_task_count",
        "hidden_subagent_count",
        "stream_index",
        "received_chars",
    }
    metadata = {key: payload[key] for key in allowed if key in payload}
    raw_goals = payload.get("goals")
    if isinstance(raw_goals, list | tuple):
        metadata["goals"] = _public_goal_rows(raw_goals)
    raw_subagents = payload.get("subagents")
    if isinstance(raw_subagents, list | tuple):
        metadata["subagents"] = _public_subagent_rows(raw_subagents)
    raw_main = payload.get("main_activity")
    if isinstance(raw_main, Mapping):
        metadata["main_activity"] = _public_main_activity(raw_main)
    return metadata


_PUBLIC_MAIN_ACTIVITY_FIELDS = frozenset(
    {"task_id", "phase", "activity", "started_at", "updated_at", "ended_at"}
)

_PUBLIC_GOAL_FIELDS = frozenset(
    {
        "goal_id",
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


# LLM: Goal display metadata repeats the runtime scalar whitelist. It is a
# read-only projection and intentionally excludes task ids, paths, wake state,
# and control authority.
# 函数用途: 保留底部 Goal 状态行和展开详情需要的公开字段。
def _public_goal_rows(value: list[object] | tuple[object, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in value[:16]:
        if not isinstance(item, Mapping):
            continue
        row = {
            str(key): item[key]
            for key in _PUBLIC_GOAL_FIELDS
            if key in item and not isinstance(item[key], dict | list | tuple | set)
        }
        if str(row.get("goal_id") or "").strip():
            rows.append(row)
    return rows


# LLM: Replay/test-injected main activity receives the same scalar whitelist as
# the live runtime so hidden output or nested payloads cannot enter rendering.
# 函数用途: 保留固定 main 行真正需要的公开字段。
def _public_main_activity(value: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value[key]
        for key in _PUBLIC_MAIN_ACTIVITY_FIELDS
        if key in value and not isinstance(value[key], dict | list | tuple | set)
    }


_PUBLIC_SUBAGENT_FIELDS = frozenset(
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
        "attempts",
        "context_tokens",
        "compact_count",
        "progress_item_ids",
        "created_at",
        "updated_at",
        "heartbeat_at",
        "ended_at",
    }
)


# LLM: The reducer repeats the runtime whitelist so replayed or test-injected
# events cannot smuggle paths, permissions, runtime activity, or nested tool
# output into a render block. Description is bounded display prose and only
# progress ids are accepted as a public list.
# 函数用途: 保留直属子代理面板的职责短标题、公开标量和 Todo 关联 ID。
def _public_subagent_rows(value: list[object] | tuple[object, ...]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in value[:64]:
        if not isinstance(item, Mapping):
            continue
        row = {
            str(key): item[key]
            for key in _PUBLIC_SUBAGENT_FIELDS
            if key in item
            and not isinstance(item[key], dict | list | tuple | set)
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
    return rows


# LLM: 工具终态文案只从 typed error_code/phase 和脱敏 output 选择；开始阶段的 command detail 不能掩盖拒绝或取消。
# 函数用途: 为完成、失败和用户拒绝生成明确的工具结果子行。
def _tool_terminal_detail(event: TuiEvent, active: TuiBlock) -> str:
    error_code = str(event.payload.get("error_code") or "").strip().upper()
    if error_code == "APPROVAL_REJECTED":
        return "用户拒绝了工具调用"
    if error_code == "CANCELLED" or event.phase == "interrupted":
        return "已中断"
    return str(
        event.payload.get("output")
        or event.payload.get("detail")
        or active.detail
    )


# LLM: queue 排序只使用显式 priority 和 seq，文本内容不影响执行/显示顺序。
# 函数用途: 生成 now>next>later、同级 FIFO 的排序键。
def _queue_sort_key(item: TuiQueuedInput) -> tuple[int, int]:
    rank = {"now": 0, "next": 1, "later": 2}.get(item.priority, 1)
    return rank, item.seq


# LLM: terminal-only 恢复只认显式内容字段存在，不读取值的语义。
# 函数用途: 判断终态事件是否携带足以重建 block 的完整投影。
def _terminal_has_content(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("text", "detail", "output", "title", "tool"))


# LLM: 数值合并只做非负整数校验，畸形值保留上一结构化事实。
# 函数用途: 安全读取 status 数值字段。
def _nonnegative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "TERMINAL_BLOCK_PHASES",
    "TuiBlock",
    "TuiContextUsage",
    "TuiDiagnostic",
    "TuiPermissionOverlay",
    "TuiPendingSteer",
    "TuiQueuedInput",
    "TuiStateStore",
    "TuiStatus",
    "TuiViewModelReducer",
    "TuiViewSnapshot",
]
