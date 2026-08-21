# LLM: 本模块是 typed TUI event 到可渲染快照的唯一 reducer；不得读取模型正文猜状态，也不得执行工具、权限或会话动作。
# 模块用途: 管理稳定历史块、活动块、权限覆盖层、输入队列、状态和有界诊断，并向 UI 提供线程安全快照。

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .tui_events import JournalAppendResult, TuiEvent, TuiEventJournal

TERMINAL_BLOCK_PHASES = frozenset({"completed", "failed", "interrupted"})


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


# LLM: TuiViewSnapshot 是 UI 线程读取的不可变投影，调用方不得直接修改 reducer 内部状态。
# 类用途: 一次性取得历史、活动块、队列、overlay、status 和 diagnostics。
@dataclass(frozen=True)
class TuiViewSnapshot:
    stable_blocks: tuple[TuiBlock, ...]
    active_blocks: tuple[TuiBlock, ...]
    pending_steers: tuple[TuiPendingSteer, ...]
    queued_inputs: tuple[TuiQueuedInput, ...]
    permission: TuiPermissionOverlay | None
    status: TuiStatus
    diagnostics: tuple[TuiDiagnostic, ...]


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
    # 函数用途: 初始化空 view model 和事件处理表。
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
        self.diagnostics: list[TuiDiagnostic] = []
        self.max_diagnostics = max(1, int(max_diagnostics or 1))
        self.max_stable_blocks = max(1, int(max_stable_blocks or 1))
        self._stable_ids: set[str] = set()
        self._handlers: dict[str, Callable[[TuiEvent], None]] = {
            "session_started": self._handle_session_started,
            "connection_started": self._handle_connection_started,
            "connection_resolved": self._handle_connection_resolved,
            "user_message": self._handle_user_message,
            "system_message": self._handle_system_message,
            "interrupt_notice": self._handle_system_message,
            "compact_boundary": self._handle_system_message,
            "context_window_compacted": self._handle_system_message,
            "conversation_compaction_started": self._handle_compact_started,
            "conversation_compaction_progress": self._handle_compact_progress,
            "conversation_compaction_completed": self._handle_compact_terminal,
            "conversation_compaction_failed": self._handle_compact_terminal,
            "assistant_started": self._handle_block_started,
            "assistant_delta": self._handle_block_delta,
            "assistant_completed": self._handle_block_completed,
            "thinking_started": self._handle_block_started,
            "thinking_delta": self._handle_block_delta,
            "thinking_completed": self._handle_block_completed,
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
        }

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

    # LLM: 活动时间只由已识别 typed event 的 created_at 推进；它只影响 spinner stall 投影，不改变 turn/tool 业务状态。
    # 函数用途: 在运行或中断中的回合收到新事件后刷新最后活动时间。
    def _touch_active_status(self, event: TuiEvent) -> None:
        if (
            self.status.phase in {"running", "interrupting"}
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
    # 函数用途: 返回当前不可变渲染快照。
    def snapshot(self) -> TuiViewSnapshot:
        return TuiViewSnapshot(
            stable_blocks=tuple(self.stable_blocks),
            active_blocks=tuple(self.active_blocks.values()),
            pending_steers=tuple(self.pending_steers),
            queued_inputs=tuple(self.queued_inputs),
            permission=self.permission,
            status=self.status,
            diagnostics=tuple(self.diagnostics),
        )

    # LLM: session_started 只生成一次欢迎块；同 block 重放由 stable id 幂等。
    # 函数用途: 建立带品牌/版本/模型/目录元数据的启动块。
    def _handle_session_started(self, event: TuiEvent) -> None:
        block = self._block_from_event(event, role="system", phase="completed")
        self._append_stable(block, event)

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

    # LLM: 完成的进度块由 canonical compact_boundary 代替为稳定历史；失败/中断则冻结一条显示证据。
    # 函数用途: 收起已完成 Compact 进度条，或保留失败提示。
    def _handle_compact_terminal(self, event: TuiEvent) -> None:
        active = self.active_blocks.pop(event.block_id, None)
        if active is None:
            self.record_diagnostic("COMPACT_TERMINAL_WITHOUT_START", event)
            return
        if event.kind == "conversation_compaction_completed":
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

    # LLM: terminal event 优先采用显式完整 text；无 active 时仅允许带完整正文的响应文件恢复创建。
    # 函数用途: 将 assistant/thinking 活动块原子冻结为稳定历史。
    def _handle_block_completed(self, event: TuiEvent) -> None:
        self._freeze_block(event, role=_role_for_kind(event.kind))

    # LLM: tool_started 只使用结构化 tool/title/detail 字段，不解析 legacy 文本。
    # 函数用途: 创建一个运行中的工具卡片。
    def _handle_tool_started(self, event: TuiEvent) -> None:
        if event.block_id in self._stable_ids or event.block_id in self.active_blocks:
            self.record_diagnostic("BLOCK_RESTART_REJECTED", event)
            return
        self.active_blocks[event.block_id] = self._block_from_event(
            event,
            role="tool",
            phase="started",
        )

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

    # LLM: gateway 对同一工具调用会发 started→…→终态多条 tool_progress 事件，
    # 终态（phase=finished/completed）被 TUI adapter 路由成 tool_completed（只走
    # terminal，不走 progress）。task_progress 的 items 只挂在终态事件上
    # （S-TP1 真机实锤），所以 terminal 也必须消费，否则 todo 面板永远无数据。
    # 函数用途: 从任意工具事件里消费 task_progress items 并更新 todo 面板。
    def _consume_task_progress_items(self, event: TuiEvent) -> None:
        items = event.payload.get("task_progress_items")
        if isinstance(items, list) and items:
            self._update_todo_block(items, event.seq)

    # LLM: todo 面板是 task_progress 账本的持续投影; 每次 items 更新替换整块
    # (block 内容即快照, 不增量合并, 避免跨轮残留旧项)。
    # 函数用途: 更新 todo 面板块(role=todo)。
    def _update_todo_block(self, items: list[dict[str, Any]], seq: int) -> None:
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

    # LLM: Promotion requires an existing exact pending id and atomically replaces its fixed
    # receipt with one stable user block in the active turn's event order.
    # 函数用途: 模型运行时确认收到补充消息后，将它从等待区移入对话历史。
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
        self._append_stable(
            self._block_from_event(promoted, role="user", phase="completed"),
            promoted,
        )

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

    # LLM: turn 状态只由 kind 映射，payload 文案不参与 running/failed/interrupted 判断。
    # 函数用途: 更新当前回合状态和事件时间。
    def _handle_turn_status(self, event: TuiEvent) -> None:
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

    # LLM: publish 的 journal append 与 reducer apply 必须原子，duplicate/rejected 不触发状态重复变化。
    # 函数用途: 发布一条事件并通知界面刷新。
    def publish(self, event: TuiEvent) -> JournalAppendResult:
        with self._lock:
            result = self.journal.append(event)
            if result.accepted:
                self.reducer.apply(event)
            elif result.status == "rejected":
                self.reducer.record_diagnostic(result.reason.upper(), event)
            subscribers = tuple(self._subscribers) if result.accepted else ()
        for callback in subscribers:
            callback()
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
        "before_tokens",
        "after_tokens",
        "trigger_tokens",
        "dropped_pairs",
        "preserved_pairs",
        "stage",
        "percent",
        "source_messages",
        "process",
    }
    return {key: payload[key] for key in allowed if key in payload}


# LLM: 工具终态文案只从 typed error_code/phase 和脱敏 output 选择；开始阶段的 command detail 不能掩盖拒绝或取消。
# 函数用途: 为完成、失败和用户拒绝生成明确的工具结果子行。
def _tool_terminal_detail(event: TuiEvent, active: TuiBlock) -> str:
    error_code = str(event.payload.get("error_code") or "").strip().upper()
    if error_code == "APPROVAL_REJECTED":
        return "User rejected tool use"
    if error_code == "CANCELLED" or event.phase == "interrupted":
        return "Interrupted"
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
