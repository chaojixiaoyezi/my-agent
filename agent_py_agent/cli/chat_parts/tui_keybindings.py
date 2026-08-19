
# LLM: 本模块把 prompt_toolkit 按键转换为输入、队列、滚动、显示模式和结构化控制动作；不得直接改 transcript 文本。
# 模块用途: 定义 chat TUI 的提交、多行编辑、中断、退出、历史视图和模式快捷键。

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from queue import Queue
from typing import Any

from .chat_style import CHAT_RESPONSE_STYLE_INJECT
from .input_loop import is_show_prompt_command
from .plain_state import ChatJob
from .tui import (
    TuiExitRefs,
    _tui_handle_command,
    _tui_request_exit,
)
from .tui_input import (
    TuiCompletion,
    apply_selected_completion,
    move_completion_selection,
    move_input_cursor_by_wrapped_rows,
    restore_queued_prompts,
    selected_completion,
)
from .tui_interaction import TuiDraft, TuiInteractionState
from .tui_params import TuiHandleCommandParams
from .tui_transcript import TuiTranscriptModeState

TRANSCRIPT_SCROLL_LINES = 10
PASTE_FEEDBACK_SECONDS = 0.1
ACTIVE_TURN_RETRY_INITIAL_SECONDS = 0.25
ACTIVE_TURN_RETRY_MAX_SECONDS = 3.0
# 单条消息字符上限（会话运行时 同款 2^20）：超限拒发且保留原文，绝不静默丢失。
MAX_USER_INPUT_CHARS = 1 << 20


# LLM: 参数束中的 tui_runtime 与 transcript view 是唯一显示入口；计时 refs 只实现双击退出，不代表业务状态。
# 类用途: 汇总输入控制器需要的控件、队列、运行快照、会话与 typed TUI runtime。
@dataclass
class TuiCreateKeybindingsParams:
    """bundle for creating TUI keybindings without growing setup signatures."""

    input_area: Any
    transcript_area: Any | None
    transcript_follow_ref: list[bool] | None
    agent: Any
    args: Any
    runtime_inject: list[Any]
    prompt_files: list[Any]
    use_gateway: bool
    paths: Any
    state_lock: threading.Lock
    is_running_ref: list[Any]
    pending_jobs_ref: list[int]
    running_prompt_ref: list[str]
    running_request_id_ref: list[str]
    running_started_at_ref: list[float]
    shutting_down_ref: list[bool]
    stop_event: threading.Event
    assistant_outputs: list[str]
    jobs: Queue[Any]
    pending_jobs_ref_for_enqueue: list[int]
    current_session_id: str
    interaction_state: Any
    history_search_area: Any
    transcript_state: Any
    transcript_search_area: Any
    permission_feedback_area: Any
    transcript_scroll_lines: int = TRANSCRIPT_SCROLL_LINES
    tui_runtime: Any | None = None
    active_input_reconciler: Any | None = None
    control_operation_reconciler: Any | None = None
    exit_armed_at_ref: list[float] | None = field(default_factory=lambda: [0.0])
    eof_armed_at_ref: list[float] | None = field(default_factory=lambda: [0.0])
    escape_armed_at_ref: list[float] | None = field(default_factory=lambda: [0.0])
    escape_armed_text_ref: list[str] | None = field(default_factory=lambda: [""])


# LLM: 这些 filters 是同一次 key map 构建的不可变条件集合；不得复制条件表达式或从控件可见文本推断 mode。
# 类用途: 集中保存聊天、历史、权限与 transcript 模式的 prompt_toolkit 条件。
@dataclass(frozen=True)
class _TuiBindingFilters:
    history_search_active: Any
    transcript_active: Any
    transcript_search_active: Any
    permission_active: Any
    permission_feedback_active: Any
    transcript_navigation: Any
    chat_active: Any
    permission_select_active: Any
    help_open: Any


# LLM: mode filters 必须只读取 typed interaction/runtime state；组合结果由全部注册 helper 共享，避免键位间状态漂移。
# 函数用途: 创建本次快捷键集合使用的模式条件。
def _make_binding_filters(params: TuiCreateKeybindingsParams, condition_type: Any) -> _TuiBindingFilters:
    history_search_active = condition_type(lambda: _history_search_active(params))
    transcript_active = condition_type(lambda: _transcript_active(params))
    transcript_search_active = condition_type(lambda: _transcript_search_active(params))
    permission_active = condition_type(lambda: _permission_active(params))
    permission_feedback_active = condition_type(
        lambda: _permission_feedback_active(params)
    )
    help_open = condition_type(
        lambda: _required_interaction(params).snapshot().help_open
    )
    return _TuiBindingFilters(
        history_search_active=history_search_active,
        transcript_active=transcript_active,
        transcript_search_active=transcript_search_active,
        permission_active=permission_active,
        permission_feedback_active=permission_feedback_active,
        transcript_navigation=transcript_active & ~transcript_search_active,
        chat_active=(
            ~history_search_active & ~transcript_active & ~permission_active
        ),
        permission_select_active=permission_active & ~permission_feedback_active,
        help_open=help_open,
    )


# LLM: permission bindings 只能发送 typed approval/cancel 事件；y/n 匹配 decision、数字键冻结 index，均不能解析 label 或落入隐藏聊天输入。
# 函数用途: 注册权限选择、补充说明、y/n/数字确认与取消快捷键。
def _register_permission_bindings(
    kb: Any,
    params: TuiCreateKeybindingsParams,
    filters: _TuiBindingFilters,
) -> None:
    active = filters.permission_active
    kb.add("up", filter=active)(lambda e: _move_permission_selection(e, params, -1))
    kb.add("down", filter=active)(lambda e: _move_permission_selection(e, params, 1))
    kb.add("c-p", filter=active)(lambda e: _move_permission_selection(e, params, -1))
    kb.add("c-n", filter=active)(lambda e: _move_permission_selection(e, params, 1))
    kb.add("c-i", filter=active)(lambda e: _toggle_permission_feedback(e, params))
    kb.add("enter", filter=active)(lambda e: _resolve_selected_permission(e, params))
    kb.add("y", filter=filters.permission_select_active)(
        lambda e: _resolve_permission_decision(e, params, ("approved",))
    )
    kb.add("n", filter=filters.permission_select_active)(
        lambda e: _resolve_permission_decision(e, params, ("denied", "cancelled"))
    )
    kb.add("escape", filter=active, eager=True)(lambda e: _cancel_permission(e, params))
    kb.add("c-c", filter=active)(lambda e: _cancel_permission(e, params))
    for option_number in range(1, 10):
        kb.add(str(option_number), filter=filters.permission_select_active)(
            lambda e, index=option_number - 1: _resolve_permission_index(
                e,
                params,
                index,
            )
        )


# LLM: chat/history bindings 共用 typed draft 与 canonical queue；普通编辑仍由 TextArea 默认 key map 负责。
# 函数用途: 注册发送、多行、补全、粘贴、草稿、退出与历史搜索快捷键。
def _register_chat_bindings(
    kb: Any,
    params: TuiCreateKeybindingsParams,
    filters: _TuiBindingFilters,
    keys: Any,
    condition_type: Any,
) -> None:
    chat_active = filters.chat_active
    history_active = filters.history_search_active
    kb.add("enter", filter=chat_active)(lambda e: _handle_enter_keybinding(e, params))
    kb.add("c-i", filter=chat_active)(lambda e: _handle_tab_keybinding(e, params))
    kb.add("c-j", filter=chat_active)(lambda e: _insert_newline(e, params))
    kb.add("escape", "enter", filter=chat_active)(lambda e: _insert_newline(e, params))
    kb.add("escape", filter=chat_active)(lambda e: _handle_escape_keybinding(e, params))
    kb.add("c-c", filter=chat_active)(lambda e: _handle_ctrl_c_keybinding(e, params))
    kb.add("c-v", filter=chat_active)(lambda e: _handle_clipboard_paste(e, params))
    kb.add("c-d", filter=chat_active)(lambda e: _handle_ctrl_d_keybinding(e, params))
    kb.add("c-s", filter=chat_active)(lambda e: _handle_stash_keybinding(e, params))
    kb.add("c-r", filter=chat_active)(lambda e: _start_history_search(e, params))
    kb.add(keys.BracketedPaste, filter=chat_active)(
        lambda e: _handle_bracketed_paste(e, params)
    )
    kb.add("c-r", filter=history_active)(lambda e: _next_history_search_match(e, params))
    kb.add("escape", filter=history_active, eager=True)(
        lambda e: _accept_history_search(e, params)
    )
    kb.add("c-i", filter=history_active)(lambda e: _accept_history_search(e, params))
    kb.add("c-c", filter=history_active)(lambda e: _cancel_history_search(e, params))
    kb.add("enter", filter=history_active)(lambda e: _execute_history_search(e, params))
    empty_history_query = condition_type(
        lambda: not str(params.history_search_area.text or "")
    )
    kb.add("backspace", filter=history_active & empty_history_query)(
        lambda e: _cancel_history_search(e, params)
    )
    kb.add("escape", "r", filter=chat_active)(lambda e: _handle_alt_r_keybinding(e, params))
    kb.add("up", filter=chat_active)(lambda e: _handle_up_keybinding(e, params))
    kb.add("down", filter=chat_active)(lambda e: _handle_down_keybinding(e, params))
    kb.add("c-p", filter=chat_active)(lambda e: _handle_completion_or_history(e, params, -1))
    kb.add("c-n", filter=chat_active)(lambda e: _handle_completion_or_history(e, params, 1))


# LLM: `?` 帮助只在空编辑器切换且不写 history；面板打开后 Enter/Esc/空删除键优先关闭，普通字符仍交给 Buffer。
# 函数用途: 注册 终端交互 输入框底部帮助面板的打开与关闭习惯。
def _register_help_bindings(
    kb: Any,
    params: TuiCreateKeybindingsParams,
    filters: _TuiBindingFilters,
    condition_type: Any,
) -> None:
    empty_input = condition_type(lambda: not str(params.input_area.text or ""))
    kb.add("?", filter=filters.chat_active & empty_input)(
        lambda e: _toggle_help(e, params)
    )
    kb.add("enter", filter=filters.help_open)(lambda e: _close_help(e, params))
    kb.add("escape", filter=filters.help_open, eager=True)(
        lambda e: _close_help(e, params)
    )
    kb.add("backspace", filter=filters.help_open & empty_input)(
        lambda e: _close_help(e, params)
    )
    kb.add("delete", filter=filters.help_open & empty_input)(
        lambda e: _close_help(e, params)
    )


# LLM: transcript bindings 只操作 typed modal/search state 和 view scroll；退出模式不提交输入或修改持久历史。
# 函数用途: 注册 transcript 模式的退出、搜索、跳转和逐行滚动快捷键。
def _register_transcript_bindings(
    kb: Any,
    params: TuiCreateKeybindingsParams,
    filters: _TuiBindingFilters,
) -> None:
    navigation = filters.transcript_navigation
    search_active = filters.transcript_search_active
    kb.add("c-e", filter=navigation)(lambda e: _handle_transcript_show_all(e, params))
    kb.add("q", filter=navigation)(lambda e: _exit_transcript_mode(e, params))
    kb.add("escape", filter=navigation, eager=True)(lambda e: _exit_transcript_mode(e, params))
    kb.add("c-c", filter=navigation)(lambda e: _handle_transcript_ctrl_c(e, params))
    kb.add("/", filter=navigation)(lambda e: _open_transcript_search(e, params))
    kb.add("n", filter=navigation)(
        lambda e: _navigate_transcript_search(e, params, reverse=False)
    )
    kb.add("N", filter=navigation)(
        lambda e: _navigate_transcript_search(e, params, reverse=True)
    )
    kb.add("up", filter=navigation)(
        lambda e: _scroll_transcript(params, -max(1, int(e.arg)))
    )
    kb.add("down", filter=navigation)(
        lambda e: _scroll_transcript(params, max(1, int(e.arg)))
    )
    kb.add("home", filter=navigation)(lambda e: _scroll_transcript_home(params))
    kb.add("end", filter=navigation)(lambda e: _scroll_transcript_end(params))
    kb.add("enter", filter=search_active)(lambda e: _commit_transcript_search(e, params))
    kb.add("escape", filter=search_active, eager=True)(
        lambda e: _cancel_transcript_search(e, params)
    )
    kb.add("c-c", filter=search_active)(lambda e: _cancel_transcript_search(e, params))
    kb.add("c-g", filter=search_active)(lambda e: _cancel_transcript_search(e, params))


# LLM: scroll bindings 只在存在 transcript view 时注册；审批 overlay 不得阻断 Page/wheel 查看上文，只有搜索栏接管时暂停。
# 函数用途: 注册翻页、首尾和鼠标滚轮快捷键。
def _register_scroll_bindings(
    kb: Any,
    params: TuiCreateKeybindingsParams,
    filters: _TuiBindingFilters,
    keys: Any,
    condition_type: Any,
) -> None:
    if params.transcript_area is None:
        return
    scroll_active = condition_type(lambda: _transcript_scroll_active(params))
    kb.add("pageup", filter=scroll_active)(
        lambda e: _scroll_transcript(params, -params.transcript_scroll_lines)
    )
    kb.add("pagedown", filter=scroll_active)(
        lambda e: _scroll_transcript(params, params.transcript_scroll_lines)
    )
    kb.add(keys.ControlHome, filter=scroll_active)(lambda e: _scroll_transcript_home(params))
    kb.add(keys.ControlEnd, filter=scroll_active)(lambda e: _scroll_transcript_end(params))
    kb.add(keys.ScrollUp, filter=scroll_active)(lambda e: _scroll_transcript(params, -3))
    kb.add(keys.ScrollDown, filter=scroll_active)(lambda e: _scroll_transcript(params, 3))


# LLM: 全局 bindings 只接管 终端交互 明确行为；普通 Emacs 编辑键继续交给 TextArea 默认 key map。
# 函数用途: 创建对话输入和 transcript 导航的快捷键集合。
def _tui_create_keybindings(params: TuiCreateKeybindingsParams):
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    kb = KeyBindings()
    if params.use_gateway:
        _ensure_active_input_reconciler(params)
        _ensure_control_operation_reconciler(params)
    filters = _make_binding_filters(params, Condition)
    _register_permission_bindings(kb, params, filters)
    _register_chat_bindings(kb, params, filters, Keys, Condition)
    _register_help_bindings(kb, params, filters, Condition)
    kb.add("c-l")(lambda e: _handle_ctrl_l_keybinding(e, params))
    kb.add(
        "c-o",
        filter=~filters.history_search_active & ~filters.permission_active,
    )(lambda e: _handle_ctrl_o_keybinding(e, params))
    kb.add("c-e", filter=filters.chat_active)(
        lambda e: _handle_ctrl_e_keybinding(e, params)
    )
    _register_transcript_bindings(kb, params, filters)
    _register_scroll_bindings(kb, params, filters, Keys, Condition)
    return kb


# LLM: help toggle 只修改 TuiInteractionState 并请求重绘；`?` 不得成为 prompt、slash command 或历史正文。
# 函数用途: 在空输入时切换快捷键帮助。
def _toggle_help(event, params: TuiCreateKeybindingsParams) -> None:
    _required_interaction(params).toggle_help()
    _reset_exit_arms(params)
    event.app.invalidate()


# LLM: 多个关闭按键复用幂等 interaction 方法，关闭帮助不触发提交或清空其它草稿状态。
# 函数用途: 收起快捷键帮助并刷新界面。
def _close_help(event, params: TuiCreateKeybindingsParams) -> None:
    _required_interaction(params).close_help()
    event.app.invalidate()


# LLM: Enter 在尾部反斜杠时只插入 newline；其余非空输入经 command dispatcher 或唯一 prompt queue 提交。
# 函数用途: 处理普通 Enter 的换行约定和消息发送。
def _handle_enter_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    buffer = params.input_area.buffer
    completion = selected_completion(buffer)
    if completion is not None:
        action = completion.enter_action if isinstance(completion, TuiCompletion) else "apply"
        if action == "submit_original":
            buffer.cancel_completion()
        else:
            apply_selected_completion(buffer)
            if action == "submit":
                _submit_input_area(event, params)
            event.app.invalidate()
            return
    _submit_input_area(event, params)


# LLM: 提交入口统一保存历史、执行 typed command 或进入 canonical queue；补全 Enter 也必须复用它。
# 函数用途: 提交当前输入框内容，并在命令未消费时创建一个聊天任务。
def _submit_input_area(event, params: TuiCreateKeybindingsParams) -> None:
    if _replace_trailing_backslash_with_newline(params.input_area):
        return
    interaction = _required_interaction(params)
    buffer = params.input_area.buffer
    draft = interaction.capture_draft(
        str(buffer.text or ""),
        int(buffer.cursor_position),
    )
    display_text = draft.text.strip()
    if not display_text:
        return
    text = interaction.expand_draft(draft).strip()
    if len(text) > MAX_USER_INPUT_CHARS:
        # 会话运行时 语义：超限拒发 + 保留原文（不丢输入），避免大粘贴静默丢失。
        _set_input_draft(params, TuiDraft(str(buffer.text or ""), int(buffer.cursor_position)))
        _required_tui_runtime(params).set_notice(
            f"消息超过 {MAX_USER_INPUT_CHARS} 字符上限（{len(text)}），未发送",
            duration_seconds=2.4,
        )
        event.app.invalidate()
        return
    _remember_input(params.input_area, text)
    _set_input_draft(params, TuiDraft("", 0))
    _reset_exit_arms(params)
    if _tui_submit_control_operation(params, text):
        _restore_stash_after_submit(params)
        event.app.invalidate()
        return
    if _tui_handle_command(params=_handle_command_params(params, text)):
        if params.stop_event.is_set():
            event.app.exit()
        else:
            _restore_stash_after_submit(params)
        return
    if _tui_submit_active_turn_input(params, text, display_text=display_text):
        _restore_stash_after_submit(params)
        event.app.invalidate()
        return
    _tui_enqueue_job(params, text, display_text=display_text)
    _restore_stash_after_submit(params)


# LLM: Tab 只接受当前候选、不执行命令；候选不存在时保留默认的字面 tab 抑制行为。
# 函数用途: 将菜单选中项写入输入框，供用户继续编辑参数或正文。
def _handle_tab_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    if apply_selected_completion(params.input_area.buffer) is not None:
        _reset_exit_arms(params)
        event.app.invalidate()


# LLM: Meta-Enter/Ctrl-J 只编辑当前 buffer，不发布用户事件或改变 canonical queue。
# 函数用途: 在输入光标处插入一个换行。
def _insert_newline(event, params: TuiCreateKeybindingsParams) -> None:
    del event
    params.input_area.buffer.insert_text("\n")
    _reset_exit_arms(params)


# LLM: 反斜杠换行只看光标前一个精确字符；不会扫描整段自然语言或改变其它反斜杠。
# 函数用途: 删除光标前的尾部反斜杠并插入换行，匹配 终端交互 输入习惯。
def _replace_trailing_backslash_with_newline(input_area: Any) -> bool:
    buffer = input_area.buffer
    before = buffer.document.text_before_cursor
    if not before.endswith("\\"):
        return False
    buffer.delete_before_cursor(count=1)
    buffer.insert_text("\n")
    return True


# LLM: FileHistory 只登记最终提交的非空输入；展示/命令分流仍由后续 typed 路径决定。
# 函数用途: 把一次提交保存到输入历史供 Up/Down 和自动建议使用。
def _remember_input(input_area: Any, text: str) -> None:
    history = getattr(input_area.buffer, "history", None)
    append = getattr(history, "append_string", None)
    if callable(append):
        append(text)


# LLM: interaction dependency 必须是 setup 创建的同一实例；缺失时禁止静默退化为无 stash/search 的另一条输入路径。
# 函数用途: 校验并返回当前 TUI 输入交互状态机。
def _required_interaction(params: TuiCreateKeybindingsParams) -> TuiInteractionState:
    if not isinstance(params.interaction_state, TuiInteractionState):
        raise TypeError("TuiCreateKeybindingsParams.interaction_state must be TuiInteractionState")
    return params.interaction_state


# LLM: active 判断只读取结构化 snapshot，不根据 footer 文案或当前 focus 猜搜索模式。
# 函数用途: 判断 Ctrl-R 历史搜索是否正在接管输入。
def _history_search_active(params: TuiCreateKeybindingsParams) -> bool:
    return _required_interaction(params).snapshot().history_search_active


# LLM: transcript dependency 必须是 setup 创建的唯一 UI mode state；缺失时不能退回 runtime 布尔开关形成第二事实源。
# 函数用途: 校验并返回详细 transcript 状态机。
def _required_transcript_state(
    params: TuiCreateKeybindingsParams,
) -> TuiTranscriptModeState:
    if not isinstance(params.transcript_state, TuiTranscriptModeState):
        raise TypeError(
            "TuiCreateKeybindingsParams.transcript_state must be "
            "TuiTranscriptModeState"
        )
    return params.transcript_state


# LLM: mode 判断只读取 transcript snapshot，不根据当前 focus、footer 文案或输入是否隐藏推断。
# 函数用途: 判断 Ctrl-O transcript 是否开启。
def _transcript_active(params: TuiCreateKeybindingsParams) -> bool:
    return _required_transcript_state(params).snapshot().active


# LLM: transcript 搜索是 transcript mode 内的显式子状态；query 是否为空不决定搜索栏归属。
# 函数用途: 判断 `/` 搜索栏是否正在接管按键。
def _transcript_search_active(params: TuiCreateKeybindingsParams) -> bool:
    return _required_transcript_state(params).snapshot().search_open


# LLM: permission overlay 与 transcript scroll 是正交状态；只有搜索输入框接管导航键时才关闭底层翻页/滚轮。
# 函数用途: 判断 Page/Home/End/滚轮是否可以移动 transcript。
def _transcript_scroll_active(params: TuiCreateKeybindingsParams) -> bool:
    return not _transcript_search_active(params)


# LLM: permission active 只读 reducer overlay；运行标志、footer 文案或焦点都不能替代这份结构化事实。
# 函数用途: 判断工具审批面板是否正在接管输入。
def _permission_active(params: TuiCreateKeybindingsParams) -> bool:
    return _required_tui_runtime(params).store.snapshot().permission is not None


# LLM: feedback mode 是 permission overlay 的显式字段，不能根据 TextArea 是否有字或当前 focus 推断。
# 函数用途: 判断当前审批选项是否已展开补充说明输入。
def _permission_feedback_active(params: TuiCreateKeybindingsParams) -> bool:
    permission = _required_tui_runtime(params).store.snapshot().permission
    return bool(permission is not None and permission.feedback_mode)


# LLM: 上下选择只发送 typed selection intent；同步输入框仅投影对应 option 草稿，不产生授权。
# 函数用途: 在权限选项之间环绕移动并刷新补充说明框。
def _move_permission_selection(
    event,
    params: TuiCreateKeybindingsParams,
    delta: int,
) -> None:
    if _required_tui_runtime(params).move_permission_selection(int(delta) * event.arg):
        _sync_permission_feedback_text(params)
        event.app.invalidate()


# LLM: Tab 只能切换当前选项的结构化 feedback mode；输入内容仍是普通上下文，不能改变 decision。
# 函数用途: 展开或收起权限补充说明输入并保持焦点在模态控件。
def _toggle_permission_feedback(event, params: TuiCreateKeybindingsParams) -> None:
    if not _required_tui_runtime(params).toggle_permission_feedback():
        return
    _sync_permission_feedback_text(params)
    event.app.layout.focus(params.permission_feedback_area)
    event.app.invalidate()


# LLM: Enter 只读取当前 option 的显式 decision；label/序号/feedback 都不得被解释成批准值。
# 函数用途: 确认当前高亮的权限选择，并附带该选项的补充说明。
def _resolve_selected_permission(event, params: TuiCreateKeybindingsParams) -> None:
    runtime = _required_tui_runtime(params)
    permission = runtime.store.snapshot().permission
    if permission is None or not permission.options:
        return
    option = permission.options[permission.selected_index]
    decision = str(option.get("decision") or "").strip().lower()
    feedback = str(params.permission_feedback_area.text or "")
    if runtime.resolve_permission(
        permission.permission_id,
        decision,
        feedback=feedback,
    ):
        _clear_permission_feedback_text(params)
        event.app.layout.focus(params.input_area)
        event.app.invalidate()


# LLM: 数字快捷键先用结构化 index 定位 option 再复用统一确认入口；越界数字不能落入隐藏 chat 输入。
# 函数用途: 通过 1-9 直接选择并确认一个权限选项。
def _resolve_permission_index(
    event,
    params: TuiCreateKeybindingsParams,
    index: int,
) -> None:
    runtime = _required_tui_runtime(params)
    permission = runtime.store.snapshot().permission
    if permission is None or index < 0 or index >= len(permission.options):
        return
    runtime.move_permission_selection(index - permission.selected_index)
    _resolve_selected_permission(event, params)


# LLM: y/n 快捷键只能匹配 option 的结构化 decision；禁止用 Yes/No label 或本地化文字推断授权结果。
# 函数用途: 按优先级查找批准或拒绝选项，移动高亮后复用统一确认入口。
def _resolve_permission_decision(
    event,
    params: TuiCreateKeybindingsParams,
    decisions: tuple[str, ...],
) -> None:
    runtime = _required_tui_runtime(params)
    permission = runtime.store.snapshot().permission
    if permission is None:
        return
    for decision in decisions:
        for index, option in enumerate(permission.options):
            if str(option.get("decision") or "").strip().lower() != decision:
                continue
            runtime.move_permission_selection(index - permission.selected_index)
            _resolve_selected_permission(event, params)
            return


# LLM: Esc/Ctrl-C 在权限模态中固定产生 cancelled，不沿用当前 Yes/No，也不触发 turn interrupt 或全局退出 arm。
# 函数用途: 取消当前工具审批并返回聊天焦点。
def _cancel_permission(event, params: TuiCreateKeybindingsParams) -> None:
    runtime = _required_tui_runtime(params)
    permission = runtime.store.snapshot().permission
    if permission is None:
        return
    if runtime.resolve_permission(permission.permission_id, "cancelled"):
        _clear_permission_feedback_text(params)
        event.app.layout.focus(params.input_area)
        event.app.invalidate()


# LLM: TextArea 内容由当前 overlay feedback 投影，程序化同步不得发布新的控制意图或修改主输入草稿。
# 函数用途: 将当前选项保存的补充说明装入权限输入框。
def _sync_permission_feedback_text(params: TuiCreateKeybindingsParams) -> None:
    from prompt_toolkit.document import Document

    permission = _required_tui_runtime(params).store.snapshot().permission
    text = permission.feedback if permission is not None else ""
    params.permission_feedback_area.buffer.set_document(
        Document(text, cursor_position=len(text)),
        bypass_readonly=True,
    )


# LLM: 清理只作用于权限 TextArea，不得清空用户主输入或历史。
# 函数用途: 在权限面板关闭后清空临时补充说明控件。
def _clear_permission_feedback_text(params: TuiCreateKeybindingsParams) -> None:
    from prompt_toolkit.document import Document

    params.permission_feedback_area.buffer.set_document(
        Document("", cursor_position=0),
        bypass_readonly=True,
    )


# LLM: Document 写入统一裁剪光标并取消旧 completion；历史/stash 恢复不得触发命令或追加 history。
# 函数用途: 把一份结构化草稿投影到主输入框。
def _set_input_draft(params: TuiCreateKeybindingsParams, draft: TuiDraft) -> None:
    from prompt_toolkit.document import Document

    buffer = params.input_area.buffer
    buffer.cancel_completion()
    _required_interaction(params).install_draft(draft)
    buffer.set_document(
        Document(draft.text, cursor_position=draft.cursor_position),
        bypass_readonly=True,
    )


# LLM: 提交恢复只消费 interaction 单槽并写回编辑器；返回的草稿不会被自动放进 canonical job queue。
# 函数用途: 在另一条命令或消息提交后自动恢复 Ctrl-S 保存的正文。
def _restore_stash_after_submit(params: TuiCreateKeybindingsParams) -> None:
    draft = _required_interaction(params).consume_stash_after_submit()
    if draft is not None:
        _set_input_draft(params, draft)


# LLM: Ctrl-S 保存精确文本/光标或在空输入时弹出已有 stash；动作不写历史、不提交模型。
# 函数用途: 实现 终端交互 的 stash/unstash 快捷键。
def _handle_stash_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    buffer = params.input_area.buffer
    interaction = _required_interaction(params)
    current = interaction.capture_draft(
        str(buffer.text or ""),
        int(buffer.cursor_position),
    )
    restored = interaction.toggle_stash(current)
    if current.text.strip():
        _set_input_draft(params, TuiDraft("", 0))
    elif restored is not None:
        _set_input_draft(params, restored)
    _reset_exit_arms(params)
    event.app.invalidate()


# LLM: 历史来源只调用当前 Buffer history 的公开 loader，并在搜索开始时冻结；读取失败等价于空候选而非改写输入。
# 函数用途: 启动 Ctrl-R 搜索并把焦点移到底栏查询框。
def _start_history_search(event, params: TuiCreateKeybindingsParams) -> None:
    buffer = params.input_area.buffer
    history = getattr(buffer, "history", None)
    loader = getattr(history, "load_history_strings", None)
    try:
        entries = tuple(loader()) if callable(loader) else tuple()
    except OSError:
        entries = tuple()
    params.history_search_area.text = ""
    buffer.cancel_completion()
    interaction = _required_interaction(params)
    interaction.start_history_search(
        interaction.capture_draft(str(buffer.text or ""), int(buffer.cursor_position)),
        entries,
    )
    event.app.layout.focus(params.history_search_area)
    _reset_exit_arms(params)
    event.app.invalidate()


# LLM: 再次 Ctrl-R 只推进冻结候选索引；无下一项时主输入保持最后 match，并由状态机显示失败标签。
# 函数用途: 在历史搜索中跳到下一条较旧匹配。
def _next_history_search_match(event, params: TuiCreateKeybindingsParams) -> None:
    draft = _required_interaction(params).next_history_match()
    if draft is not None:
        _set_input_draft(params, draft)
    event.app.invalidate()


# LLM: 结束搜索先销毁结构化 session，再清 query Buffer，避免 on_text_changed 把清空动作误当新查询。
# 函数用途: 收尾搜索、恢复主输入焦点并写入接受或取消后的草稿。
def _finish_history_search(
    event,
    params: TuiCreateKeybindingsParams,
    draft: TuiDraft,
) -> None:
    _set_input_draft(params, draft)
    params.history_search_area.text = ""
    event.app.layout.focus(params.input_area)
    _reset_exit_arms(params)
    event.app.invalidate()


# LLM: Esc/Tab 接受当前显示匹配但不提交；无匹配时恢复搜索开始前的草稿。
# 函数用途: 接受历史搜索结果并返回普通编辑模式。
def _accept_history_search(event, params: TuiCreateKeybindingsParams) -> None:
    draft = _required_interaction(params).accept_history_search()
    _finish_history_search(event, params, draft)


# LLM: Ctrl-C 取消必须精确恢复 original draft/cursor，不能沿用临时 match 或触发全局退出 arm。
# 函数用途: 取消历史搜索并返回原输入。
def _cancel_history_search(event, params: TuiCreateKeybindingsParams) -> None:
    draft = _required_interaction(params).cancel_history_search()
    _finish_history_search(event, params, draft)


# LLM: Enter 仅提交空 query 的原草稿或已找到的当前 match；零匹配时只退出搜索，不制造空任务。
# 函数用途: 接受历史搜索结果并立即走统一提交入口。
def _execute_history_search(event, params: TuiCreateKeybindingsParams) -> None:
    draft = _required_interaction(params).execute_history_search()
    if draft is None:
        current = _required_interaction(params).capture_draft(
            str(params.input_area.buffer.text or ""),
            int(params.input_area.buffer.cursor_position),
        )
        _finish_history_search(event, params, current)
        return
    _finish_history_search(event, params, draft)
    _submit_input_area(event, params)


# LLM: 粘贴规范化只删除 ANSI、统一换行并展开 tab；正文仍作为用户输入，不做命令或状态解析。
# 函数用途: 规范终端 bracketed-paste 的完整文本块。
def _normalize_bracketed_paste(text: str) -> str:
    from .renderer import strip_ansi

    normalized = strip_ansi(str(text or ""))
    return normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")


# LLM: 所有粘贴入口先替换当前显式选区，再通过同一 interaction contract 插入；不得绕过大文本 ref 或直接改 Document。
# 函数用途: 把一段剪贴板正文作为一次粘贴写入输入框，并短暂显示 `Pasting text…`。
def _insert_pasted_text(event, params: TuiCreateKeybindingsParams, text: str) -> bool:
    interaction = _required_interaction(params)
    normalized = _normalize_bracketed_paste(text)
    if not normalized:
        return False
    interaction.set_pasting(True)
    buffer = params.input_area.buffer
    if getattr(buffer, "selection_state", None) is not None:
        buffer.cut_selection()
    buffer.insert_text(interaction.register_text_paste(normalized))
    timer = threading.Timer(PASTE_FEEDBACK_SECONDS, interaction.set_pasting, args=(False,))
    timer.daemon = True
    timer.start()
    _reset_exit_arms(params)
    event.app.invalidate()
    return True


# LLM: bracketed paste 是外部终端剪贴板的主路径；事件正文必须完整交给统一粘贴入口，不能逐字符重放。
# 函数用途: 接收 Cmd-V、Ctrl-Shift-V 等终端生成的 bracketed-paste 文本块。
def _handle_bracketed_paste(event, params: TuiCreateKeybindingsParams) -> None:
    _insert_pasted_text(event, params, event.data)


# LLM: Ctrl-V 只读取 prompt_toolkit 应用剪贴板；系统剪贴板仍由终端通过 bracketed paste 注入，不能在 TUI 内猜平台命令。
# 函数用途: 把本 TUI 刚复制的文本粘贴到输入框，支持替换当前选区；应用剪贴板为空时给出可操作提示。
def _handle_clipboard_paste(event, params: TuiCreateKeybindingsParams) -> None:
    clipboard = getattr(event.app, "clipboard", None)
    get_data = getattr(clipboard, "get_data", None)
    if not callable(get_data):
        return
    data = get_data()
    text = str(getattr(data, "text", "") or "")
    if not text:
        _required_tui_runtime(params).set_notice(
            "应用剪贴板为空：系统粘贴请用 Cmd-V / Ctrl-Shift-V",
            duration_seconds=1.6,
        )
        event.app.invalidate()
        return
    _insert_pasted_text(event, params, text)


def _handle_command_params(params: TuiCreateKeybindingsParams, text: str) -> TuiHandleCommandParams:
    return TuiHandleCommandParams(
        user=text,
        agent=params.agent,
        args=params.args,
        runtime_inject=params.runtime_inject,
        prompt_files=params.prompt_files,
        use_gateway=params.use_gateway,
        paths=params.paths,
        state_lock=params.state_lock,
        is_running_ref=params.is_running_ref,
        pending_jobs_ref=params.pending_jobs_ref,
        running_prompt_ref=params.running_prompt_ref,
        running_request_id_ref=params.running_request_id_ref,
        running_started_at_ref=params.running_started_at_ref,
        shutting_down_ref=params.shutting_down_ref,
        stop_event=params.stop_event,
        assistant_outputs=params.assistant_outputs,
        current_session_id=params.current_session_id,
    )


# LLM: Execution options are snapshotted at Enter time and then travel with either the local job or
# durable active-input outbox. A reconciler/restart must never read mutable CLI lists to rebuild them.
# 函数用途: 冻结当前 TUI 请求的注入、文件、保存、恢复和交互能力选项。
def _tui_execution_options(
    params: TuiCreateKeybindingsParams,
    *,
    include_chat_style: bool = False,
):
    from ...agent.gateway_parts.request_client import GatewayAskExecutionOptions

    inject = tuple(str(item) for item in params.runtime_inject)
    if include_chat_style:
        inject = (*inject, CHAT_RESPONSE_STYLE_INJECT)
    return GatewayAskExecutionOptions(
        inject=inject,
        prompt_files=tuple(str(item) for item in params.prompt_files),
        save=not bool(getattr(params.args, "no_save", False)),
        include_prompt=False,
        resume_context=(
            getattr(params.args, "resume_context", None)
            if isinstance(getattr(params.args, "resume_context", None), bool)
            else None
        ),
        tool_approval=True,
        rich_transcript=True,
    )


# LLM: 用户块与 queue 状态必须先发布到同一 TuiRuntime，再把同 request_id job 放入唯一执行队列。
# 函数用途: 构造聊天任务、显示用户输入并按当前运行快照标记排队。
def _tui_enqueue_job(
    params: TuiCreateKeybindingsParams,
    text: str,
    *,
    display_text: str | None = None,
    execution_options=None,
    inject_complete: bool = False,
) -> None:
    from ...agent.conversation.control_commands import parse_conversation_task_command
    from ...agent.conversation.models import new_id

    options = execution_options or _tui_execution_options(params)
    visible_text = str(display_text if display_text is not None else text)
    show_prompt, text = is_show_prompt_command(text)
    task_command = parse_conversation_task_command(text)
    system_task: dict[str, object] = {}
    if task_command is not None and task_command.valid:
        text = task_command.prompt
        system_task = task_command.to_request_payload()
    job = ChatJob(
        user=text,
        show_prompt=show_prompt,
        inject=list(options.inject),
        prompt_files=list(options.prompt_files),
        request_id=new_id("chat"),
        system_task=system_task,
        display_text=visible_text,
        save=options.save,
        resume_context=options.resume_context,
        tool_approval=options.tool_approval,
        rich_transcript=options.rich_transcript,
        inject_complete=inject_complete,
    )
    with params.state_lock:
        queued = bool(params.is_running_ref[0]) or int(params.pending_jobs_ref_for_enqueue[0]) > 0
        params.pending_jobs_ref_for_enqueue[0] += 1
    runtime = _required_tui_runtime(params)
    runtime.enqueue_prompt(job.request_id, visible_text, queued=queued)
    params.jobs.put(job)


# LLM: Gateway has already created the canonical queued request. This job only attaches the
# existing request id to the one worker queue; it must never POST the user text a second time.
# 函数用途: 把 Gateway 自动转入下一轮的请求接到本地 TUI 队列中继续显示。
def _tui_attach_gateway_job(
    params: TuiCreateKeybindingsParams,
    text: str,
    *,
    display_text: str,
    gateway_request_id: str,
    client_message_id: str,
    execution_options=None,
) -> None:
    request_id = str(gateway_request_id or "").strip()
    if not request_id:
        raise ValueError("queued active-turn input requires gateway_request_id")
    options = execution_options or _tui_execution_options(params)
    job = ChatJob(
        user=text,
        show_prompt=options.include_prompt,
        inject=list(options.inject),
        prompt_files=list(options.prompt_files),
        request_id=request_id,
        display_text=display_text,
        gateway_request_id=request_id,
        client_message_id=client_message_id,
        save=options.save,
        resume_context=options.resume_context,
        tool_approval=options.tool_approval,
        rich_transcript=options.rich_transcript,
        inject_complete=True,
    )
    with params.state_lock:
        params.pending_jobs_ref_for_enqueue[0] += 1
    runtime = _required_tui_runtime(params)
    runtime.enqueue_prompt(request_id, display_text, queued=True)
    params.jobs.put(job)


# LLM: Valid Gateway slash controls enter a durable operation outbox before HTTP. `/btw` and
# `/stop` require the exact canonical turn observed at Enter; an empty/submitting id never permits
# the server to guess a later turn.
# 函数用途: 把 TUI 控制命令持久排入统一对账器，无法精确定位当前回合时就地拒绝。
def _tui_submit_control_operation(
    params: TuiCreateKeybindingsParams,
    text: str,
) -> bool:
    if not bool(getattr(params, "use_gateway", False)):
        return False
    from ...agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control(text, reject_unknown_slash=True)
    if command is None or not command.valid:
        return False
    expected_turn_id = ""
    if command.kind in {"steer", "stop"}:
        with params.state_lock:
            running = bool(params.is_running_ref[0])
            expected_turn_id = str(params.running_request_id_ref[0] or "").strip()
        if not running or not expected_turn_id:
            _required_tui_runtime(params).set_notice(
                "No exact active turn yet · command not sent",
                duration_seconds=2.5,
            )
            return True
    from ...agent.conversation.models import new_id
    from .tui_control_delivery import TuiControlOperationEntry

    message_id = new_id("control")
    try:
        _ensure_control_operation_reconciler(params).enqueue(
            TuiControlOperationEntry(
                message_id=message_id,
                command_text=str(text or "").strip(),
                command_kind=command.kind,
                expected_turn_id=expected_turn_id,
            )
        )
    except Exception:
        _required_tui_runtime(params).set_notice(
            "Control operation could not be saved",
            duration_seconds=2.5,
        )
        return True
    _required_tui_runtime(params).set_notice(
        "Confirming control operation…",
        duration_seconds=1.2,
    )
    return True


# LLM: A single durable reconciler owns every TUI control retry. It reuses the saved message id,
# freezes the exact turn snapshot, and switches permanently to GET after receiving operation_id.
# 函数用途: 创建或取得本会话控制命令对账器，并连接 Gateway 提交、查询与界面回调。
def _ensure_control_operation_reconciler(
    params: TuiCreateKeybindingsParams,
):
    existing = getattr(params, "control_operation_reconciler", None)
    if existing is not None:
        return existing
    from ...agent.conversation.control_commands import parse_conversation_control
    from .control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
        request_gateway_control_status,
    )
    from .tui_control_delivery import (
        TuiControlOperationReconciler,
        tui_control_operation_outbox_path,
    )

    # LLM: Every POST rebuilds only from the persisted row and reuses its exact client message id.
    # 函数用途: 用持久控制行向 Gateway 提交或安全重放同一命令。
    def submit(entry):
        command = parse_conversation_control(entry.command_text, reject_unknown_slash=True)
        if command is None or not command.valid or command.kind != entry.command_kind:
            from ...agent.conversation.control_commands import ConversationControlResult

            return ConversationControlResult(
                entry.command_kind,
                False,
                "持久控制命令已损坏，未发送。",
                control_state="conflict",
            )
        execution = ChatControlExecution(
            agent=params.agent,
            use_gateway=True,
            state=ChatControlState(
                running=bool(entry.expected_turn_id),
                queued_count=0,
                prompt="",
                started_at=0.0,
                session_id=str(params.current_session_id or "default"),
                request_id=entry.expected_turn_id,
            ),
        )
        return execute_chat_control(
            execution,
            command,
            message_id=entry.message_id,
        )

    # LLM: Status requests use only the persisted server operation id and cannot resend command text.
    # 函数用途: 只读查询一条 Gateway 控制操作回执。
    def status(operation_id: str):
        return request_gateway_control_status(
            ChatControlExecution(
                agent=params.agent,
                use_gateway=True,
                state=ChatControlState(
                    running=False,
                    queued_count=0,
                    prompt="",
                    started_at=0.0,
                    session_id=str(params.current_session_id or "default"),
                ),
            ),
            operation_id,
        )

    runtime = _required_tui_runtime(params)

    # LLM: Restart restoration is a notice only; command/result text stays in the server receipt.
    # 函数用途: TUI 重启时提示仍有控制操作正在对账。
    def on_restore(_entry) -> None:
        runtime.set_notice("Restoring control confirmation…", duration_seconds=2.0)

    # LLM: Completed controls produce one local projection; stop stays a compact status notice.
    # 函数用途: 展示已经有持久结果的控制命令。
    def on_complete(entry, result) -> None:
        if entry.command_kind == "stop":
            runtime.set_notice(result.message or "Stop request completed", duration_seconds=2.0)
            return
        runtime.write_console(result.message or "控制命令已完成。")

    # LLM: Terminal uncertainty is never rendered as success or retried with a fresh id.
    # 函数用途: 告知用户控制副作用无法确认且系统不会自动重复执行。
    def on_terminal_unknown(_entry, result) -> None:
        runtime.set_notice(
            result.message or "Control result is unknown · not repeated",
            duration_seconds=3.0,
        )

    # LLM: Id conflicts are quarantined; the client cannot bypass them by silently making a new id.
    # 函数用途: 提示控制消息身份冲突并停止自动重试。
    def on_conflict(_entry) -> None:
        runtime.set_notice("Control identity conflict · not repeated", duration_seconds=3.0)

    # LLM: Transient outbox errors leave canonical rows untouched and keep the sole worker alive.
    # 函数用途: 提示控制对账暂时失败，后台继续退避。
    def on_error(_error: BaseException) -> None:
        runtime.set_notice("Control confirmation is retrying…", duration_seconds=2.5)

    reconciler = TuiControlOperationReconciler(
        path=tui_control_operation_outbox_path(
            params.paths.root,
            params.current_session_id,
        ),
        submit=submit,
        status=status,
        on_restore=on_restore,
        on_complete=on_complete,
        on_terminal_unknown=on_terminal_unknown,
        on_conflict=on_conflict,
        on_error=on_error,
        stop_event=params.stop_event,
        initial_delay=ACTIVE_TURN_RETRY_INITIAL_SECONDS,
        maximum_delay=ACTIVE_TURN_RETRY_MAX_SECONDS,
    )
    params.control_operation_reconciler = reconciler
    return reconciler


# LLM: Busy Gateway submissions use one expected turn id and one opaque client id. Accepted and
# unknown both retain the pending receipt; only an explicit rejected result permits queue fallback.
# 函数用途: 运行中把普通 Enter 送进精确当前回合，并按投递三态决定等待、对账或排队。
def _tui_submit_active_turn_input(
    params: TuiCreateKeybindingsParams,
    text: str,
    *,
    display_text: str,
) -> bool:
    if not params.use_gateway:
        return False
    from ...agent.conversation.control_commands import parse_conversation_task_command

    show_prompt, _ordinary_text = is_show_prompt_command(text)
    if show_prompt or parse_conversation_task_command(text) is not None:
        return False
    with params.state_lock:
        running = bool(params.is_running_ref[0])
        expected_turn_id = str(params.running_request_id_ref[0] or "").strip()
    if not running or not expected_turn_id:
        return False
    if not callable(getattr(params.agent, "request_active_turn_input", None)):
        return False
    from ...agent.conversation.models import new_id

    message_id = new_id("steer")
    runtime = _required_tui_runtime(params)
    runtime.enqueue_active_turn_input(message_id, display_text)
    from .tui_input_delivery import TuiActiveInputOutboxEntry

    execution_options = _tui_execution_options(params, include_chat_style=True)
    reconciler = _ensure_active_input_reconciler(params)
    try:
        reconciler.enqueue(
            TuiActiveInputOutboxEntry(
                message_id=message_id,
                expected_turn_id=expected_turn_id,
                text=text,
                display_text=display_text,
                execution_options=execution_options,
            )
        )
    except Exception:
        runtime.cancel_active_turn_input(message_id)
        runtime.set_notice("Delivery state needs attention · queued next", duration_seconds=2.5)
        return False
    runtime.set_notice("Confirming delivery…", duration_seconds=1.2)
    return True


# LLM: One reconciler and one durable outbox own every uncertain input for this TUI session. The
# instance is created during keymap setup so restart recovery works before the next user message.
# 函数用途: 创建或取得会话级补充消息对账器，并接好 UI 与现有 Gateway job 队列回调。
def _ensure_active_input_reconciler(
    params: TuiCreateKeybindingsParams,
):
    existing = getattr(params, "active_input_reconciler", None)
    if existing is not None:
        return existing
    from ..chat_client_context import ActiveTurnInputDelivery, ActiveTurnInputResult
    from .tui_input_delivery import (
        TuiActiveInputReconciler,
        tui_active_input_outbox_path,
    )

    submit_method = getattr(params.agent, "request_active_turn_input", None)
    status_method = getattr(params.agent, "request_active_turn_input_status", None)

    # LLM: Submission callback always reuses the persisted message and expected-turn ids.
    # 函数用途: 向 Gateway 提交或重放同一条待确认消息。
    def submit(entry):
        if not callable(submit_method):
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        try:
            result = submit_method(
                params.current_session_id,
                message=entry.text,
                message_id=entry.message_id,
                expected_turn_id=entry.expected_turn_id,
                execution_options=entry.execution_options,
            )
        except Exception:
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        return (
            result
            if isinstance(result, ActiveTurnInputResult)
            else ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        )

    # LLM: Once the stable ingress id is known, status polling is read-only and cannot duplicate text.
    # 函数用途: 查询 Gateway 普通消息持久回执。
    def status(request_id: str):
        if not callable(status_method):
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN, request_id=request_id)
        try:
            result = status_method(request_id)
        except Exception:
            return ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN, request_id=request_id)
        return (
            result
            if isinstance(result, ActiveTurnInputResult)
            else ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN, request_id=request_id)
        )

    runtime = _required_tui_runtime(params)

    # LLM: Restart restoration recreates only the local pending block; transport identity remains in
    # the durable outbox.
    # 函数用途: TUI 重启后恢复一条“正在确认”显示。
    def on_restore(entry) -> None:
        try:
            runtime.enqueue_active_turn_input(entry.message_id, entry.display_text)
        except KeyError:
            pass

    # LLM: Durable consumed status repairs a missed stream event using the original exact turn.
    # 函数用途: 将已消费补充消息移入当前回合历史。
    def on_accepted(entry) -> None:
        runtime.promote_active_turn_inputs(
            (entry.message_id,),
            request_id=entry.expected_turn_id,
        )
        runtime.set_notice("Added to current turn", duration_seconds=1.2)

    # LLM: Queued callback attaches the existing stable Gateway request and never POSTs text again.
    # 函数用途: 撤下插入等待项并把服务器已排队请求接到本地 worker。
    def on_queued(entry, request_id: str) -> None:
        if runtime.cancel_active_turn_input(entry.message_id):
            _tui_attach_gateway_job(
                params,
                entry.text,
                display_text=entry.display_text,
                gateway_request_id=request_id,
                client_message_id=entry.message_id,
                execution_options=entry.execution_options,
            )
        runtime.set_notice("Current turn ended · queued next", duration_seconds=1.8)

    # LLM: Explicit rejection proves no Gateway side effect; only then may the ordinary queue create
    # a new request for the same visible text.
    # 函数用途: 明确拒绝时撤下等待项并安全排到下一轮。
    def on_rejected(entry) -> None:
        if runtime.cancel_active_turn_input(entry.message_id):
            _tui_enqueue_job(
                params,
                entry.text,
                display_text=entry.display_text,
                execution_options=entry.execution_options,
                inject_complete=True,
            )
        runtime.set_notice("Current turn ended · queued next", duration_seconds=1.8)

    # LLM: Reusing one immutable client id with another payload is quarantined. Retrying or queueing
    # the conflicting text would bypass the idempotency authority and create a second side effect.
    # 函数用途: 撤下冲突消息的等待显示并提示用户重新输入，不自动创建新任务。
    def on_conflict(entry) -> None:
        runtime.cancel_active_turn_input(entry.message_id)
        runtime.set_notice("Message identity conflict · please send again", duration_seconds=3.0)

    # LLM: Disk/corruption errors must not kill the sole reconciler silently; the durable row stays
    # untouched while the user receives a non-authoritative diagnostic notice.
    # 函数用途: 提示补充消息对账暂时失败，后台继续按退避重试。
    def on_error(_error: BaseException) -> None:
        runtime.set_notice("Delivery confirmation is retrying…", duration_seconds=2.5)

    reconciler = TuiActiveInputReconciler(
        path=tui_active_input_outbox_path(params.paths.root, params.current_session_id),
        submit=submit,
        status=status,
        on_restore=on_restore,
        on_accepted=on_accepted,
        on_queued=on_queued,
        on_rejected=on_rejected,
        on_conflict=on_conflict,
        on_error=on_error,
        stop_event=params.stop_event,
        initial_delay=ACTIVE_TURN_RETRY_INITIAL_SECONDS,
        maximum_delay=ACTIVE_TURN_RETRY_MAX_SECONDS,
    )
    params.active_input_reconciler = reconciler
    return reconciler


# LLM: runtime 缺失是主链接线错误，不能退回 `_cprint` 建立第二 transcript。
# 函数用途: 校验并取得 typed TUI runtime。
def _required_tui_runtime(params: TuiCreateKeybindingsParams):
    from .tui_runtime import TuiRuntime

    if not isinstance(params.tui_runtime, TuiRuntime):
        raise TypeError("TuiCreateKeybindingsParams.tui_runtime must be TuiRuntime")
    return params.tui_runtime


# LLM: Ctrl-C 先复制当前输入选区，再复制 typed transcript 选区；两者都为空时才停止回合或进入双击退出状态。
# 函数用途: 实现输入/正文复制、活动中断和防误触双击退出的明确优先级。
def _handle_ctrl_c_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    if _copy_input_selection(event, params):
        return
    if _copy_transcript_selection(event, params):
        return
    with params.state_lock:
        running = bool(params.is_running_ref[0])
        queued = int(params.pending_jobs_ref[0] or 0) > 0
    if running:
        _dispatch_active_interrupt(event, params)
        return
    if queued and _restore_editable_queue(params):
        event.app.invalidate()
        return
    if params.input_area.text:
        _set_input_draft(params, TuiDraft("", 0))
        _arm_exit(params, eof=False)
        event.app.invalidate()
        return
    if _arm_exit(params, eof=False):
        _request_exit(params)
        event.app.exit()
        return
    _required_tui_runtime(params).set_notice("Press Ctrl-C again to exit")


# LLM: 输入复制只读取 Buffer Document 的显式 selection，不调用会清空 selection_state 的 Buffer.copy_selection。
# 函数用途: 返回输入框当前选中的正文，供 Ctrl-C 和鼠标松手自动复制共用。
def _selected_input_text(buffer: Any) -> str:
    if getattr(buffer, "selection_state", None) is None:
        return ""
    _remaining, clipboard_data = buffer.document.cut_selection()
    return str(getattr(clipboard_data, "text", "") or "")


# LLM: 输入选区优先于 transcript 和中断动作；复制后保留原选区高亮，便于用户确认或直接粘贴替换。
# 函数用途: 把输入框选区复制到应用、tmux 和系统终端剪贴板投影。
def _copy_input_selection(event, params: TuiCreateKeybindingsParams) -> bool:
    selected_text = _selected_input_text(params.input_area.buffer)
    if not selected_text:
        return False
    _write_selection_clipboard(event.app, selected_text)
    _required_tui_runtime(params).set_notice(f"Copied {len(selected_text)} chars")
    event.app.invalidate()
    return True


# LLM: transcript mode 的 Ctrl-C 与普通模式共用同一选区复制入口；只有空选区才退出 modal，不得中断后台回合。
# 函数用途: 在详细 transcript 中复制选区，或在没有选区时退出浏览模式。
def _handle_transcript_ctrl_c(event, params: TuiCreateKeybindingsParams) -> None:
    if _copy_transcript_selection(event, params):
        return
    _exit_transcript_mode(event, params)


# LLM: copy 只读取 TuiTranscriptView 暴露的可见选区，并以 app clipboard + OSC 52 投影；空选区绝不吞掉 Ctrl-C。
# 函数用途: 把当前 transcript 选区复制到终端剪贴板，并显示短暂确认。
def _copy_transcript_selection(event, params: TuiCreateKeybindingsParams) -> bool:
    view = params.transcript_area
    selected_text = (
        str(view.selected_text() or "")
        if view is not None and callable(getattr(view, "selected_text", None))
        else ""
    )
    if not selected_text:
        return False
    _write_selection_clipboard(event.app, selected_text)
    _required_tui_runtime(params).set_notice(f"Copied {len(selected_text)} chars")
    event.app.invalidate()
    return True


# LLM: clipboard 输出仅编码用户已显式选中的可见文本；超大文本仍进 app clipboard，但不发送可能被终端截断的 OSC 52。
# 函数用途: 同时写 prompt_toolkit 内部剪贴板、本机系统剪贴板和终端 OSC 52 剪贴板。
def _write_selection_clipboard(application: Any, text: str) -> None:
    from prompt_toolkit.clipboard import ClipboardData

    normalized = str(text or "")
    application.clipboard.set_data(ClipboardData(normalized))
    encoded = normalized.encode("utf-8")
    if not encoded or len(encoded) > 100_000:
        return
    # native 路径先于 tmux/OSC 52 启动（终端交互 同款）：无 SSH_CONNECTION 时本机
    # 剪贴板工具直接写系统剪贴板，避免快速切走焦点后粘贴被 tmux 等待拖慢。
    if not os.environ.get("SSH_CONNECTION"):
        threading.Thread(
            target=_copy_native_clipboard,
            args=(normalized,),
            name="my-agent-tui-clipboard-native",
            daemon=True,
        ).start()
    if os.environ.get("TMUX"):
        threading.Thread(
            target=_load_tmux_clipboard_buffer,
            args=(normalized,),
            name="my-agent-tui-clipboard",
            daemon=True,
        ).start()
    output = getattr(application, "output", None)
    write_raw = getattr(output, "write_raw", None)
    if not callable(write_raw):
        return
    payload = base64.b64encode(encoded).decode("ascii")
    write_raw(_osc52_sequence(payload, inside_tmux=bool(os.environ.get("TMUX"))))
    flush = getattr(output, "flush", None)
    if callable(flush):
        flush()


# LLM: SSH 会话（tmux pane 继承 SSH_TTY）不能以 SSH_TTY 判断远端；SSH_CONNECTION 在 tmux 默认
# update-environment 中会被清除，本地 attach 后 native 自动恢复。失败静默，OSC 52 可能已成功。
# 函数用途: 用本机剪贴板工具把文本写入系统剪贴板（仅本地无 SSH 会话时；远端写的是远端剪贴板）。
def _copy_native_clipboard(text: str) -> None:
    if os.environ.get("SSH_CONNECTION"):
        return
    if sys.platform == "darwin":
        _run_clipboard_tool(["pbcopy"], text)
        return
    if sys.platform.startswith("linux"):
        tool = _linux_clipboard_tool()
        if tool is not None:
            _run_clipboard_tool(tool, text)
        return
    if sys.platform == "win32":
        _run_clipboard_tool(["clip"], text)


_linux_clipboard_tool_cache: list[str] | None = None
_linux_clipboard_tool_lock = threading.Lock()


# LLM: Linux 剪贴板工具探测结果缓存，避免每次鼠标松手重复探测（终端交互 同款缓存）。
# 函数用途: 按 Wayland→X11 顺序探测可用剪贴板命令；无可用返回 None。
def _linux_clipboard_tool() -> list[str] | None:
    global _linux_clipboard_tool_cache
    with _linux_clipboard_tool_lock:
        if _linux_clipboard_tool_cache is not None:
            return _linux_clipboard_tool_cache or None
        for candidate in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if shutil.which(candidate[0]) is not None:
                _linux_clipboard_tool_cache = candidate
                return candidate
        _linux_clipboard_tool_cache = []
        return None


def _run_clipboard_tool(args: list[str], text: str) -> bool:
    try:
        result = subprocess.run(
            args,
            input=str(text or ""),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# LLM: tmux may drop DCS passthrough when allow-passthrough is off. Its own paste buffer is the
# reliable local authority; -w additionally asks tmux to forward the same text to the outer client.
# 函数用途: 在后台把已选文本写入 tmux buffer；失败时保留 OSC 52 和应用内剪贴板兜底。
def _load_tmux_clipboard_buffer(text: str) -> bool:
    args = ["tmux", "load-buffer"]
    if os.environ.get("LC_TERMINAL") != "iTerm2":
        args.append("-w")
    args.append("-")
    try:
        result = subprocess.run(
            args,
            input=str(text or ""),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# LLM: tmux passthrough 只包裹固定 OSC 52 控制骨架和 Base64 payload；payload 不得含未编码终端控制字符。
# 函数用途: 生成普通终端或 tmux 内可转发的剪贴板控制序列。
def _osc52_sequence(payload: str, *, inside_tmux: bool) -> str:
    sequence = f"\x1b]52;c;{str(payload or '')}\x07"
    if not inside_tmux:
        return sequence
    return "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + "\x1b\\"


# LLM: Ctrl-D 在非空 buffer 保持 forward-delete；只有空输入在短窗内第二次才退出。
# 函数用途: 处理字符删除和 EOF 双击退出。
def _handle_ctrl_d_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    buffer = params.input_area.buffer
    if buffer.text:
        buffer.delete(count=1)
        _reset_exit_arms(params)
        return
    if _arm_exit(params, eof=True):
        _request_exit(params)
        event.app.exit()
        return
    _required_tui_runtime(params).set_notice("Press Ctrl-D again to exit")


# LLM: Esc 在运行中只发送 typed stop；空闲非空输入必须在 800ms 内双击且草稿未变才保存历史并清空。
# 函数用途: 处理中断当前回合，或以 终端交互 的双 Esc 防误触语义清空草稿。
def _handle_escape_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    buffer = params.input_area.buffer
    if buffer.complete_state is not None:
        buffer.cancel_completion()
        _reset_exit_arms(params)
        event.app.invalidate()
        return
    with params.state_lock:
        running = bool(params.is_running_ref[0])
        queued = int(params.pending_jobs_ref[0] or 0) > 0
    if running:
        _dispatch_active_interrupt(event, params)
        return
    if queued and _restore_editable_queue(params):
        event.app.invalidate()
        return
    text = str(params.input_area.text or "")
    if text:
        if _arm_escape(params, text):
            draft = _required_interaction(params).capture_draft(
                text,
                int(buffer.cursor_position),
            )
            _remember_input(params.input_area, _required_interaction(params).expand_draft(draft))
            _set_input_draft(params, TuiDraft("", 0))
            _required_tui_runtime(params).set_notice("")
            _reset_exit_arms(params)
        else:
            _required_tui_runtime(params).set_notice(
                "Esc again to clear",
                duration_seconds=1.0,
            )
        event.app.invalidate()
        return
    _reset_escape_arm(params)


# LLM: Up 必须先按当前渲染宽度走软折视觉行；只有到视觉顶边才允许恢复 queue，再由 Buffer 处理逻辑行/history。
# 函数用途: 在屏幕折行、真实换行、排队消息和历史之间按 终端交互 顺序向上移动。
def _handle_up_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    buffer = params.input_area.buffer
    if buffer.complete_state is not None:
        move_completion_selection(buffer, -event.arg)
        event.app.invalidate()
        return
    if move_input_cursor_by_wrapped_rows(
        buffer,
        width=_input_visual_width(event, params),
        delta=-max(1, int(event.arg)),
    ):
        _reset_exit_arms(params)
        event.app.invalidate()
        return
    if buffer.document.cursor_position_row == 0:
        if _restore_editable_queue(params):
            _reset_exit_arms(params)
            event.app.invalidate()
            return
    buffer.auto_up(count=event.arg, go_to_start_of_line_if_history_changes=True)


# LLM: queue 回取必须原子移除 canonical Queue 项并让 runtime 发 queue_restored；Esc/Ctrl-C/Up 共用同一实现避免数量与显示分叉。
# 函数用途: 将所有尚未执行的可编辑消息合并回当前输入，并保留当前粘贴引用。
def _restore_editable_queue(params: TuiCreateKeybindingsParams) -> bool:
    buffer = params.input_area.buffer
    restored = restore_queued_prompts(
        jobs=params.jobs,
        runtime=_required_tui_runtime(params),
        state_lock=params.state_lock,
        pending_jobs_ref=params.pending_jobs_ref_for_enqueue,
        current_text=buffer.text,
        current_cursor=buffer.cursor_position,
    )
    if restored is None:
        return False
    current = _required_interaction(params).capture_draft(
        str(buffer.text or ""),
        int(buffer.cursor_position),
    )
    _set_input_draft(
        params,
        TuiDraft(
            restored.text,
            restored.cursor_position,
            current.pasted_text_refs,
        ),
    )
    return True


# LLM: Gateway stop 的网络往返不得占用 prompt_toolkit UI 线程；typed interrupting 先发布，真实终态仍由 worker response 决定。
# 函数用途: 立即反馈活动中断，并按本地/Gateway 后端安全派发同一 `/stop` 控制命令。
def _dispatch_active_interrupt(event, params: TuiCreateKeybindingsParams) -> None:
    if not params.use_gateway:
        _required_tui_runtime(params).request_interrupt()
        event.app.invalidate()
        _tui_handle_command(params=_handle_command_params(params, "/stop"))
        return
    with params.state_lock:
        exact_turn_id = str(params.running_request_id_ref[0] or "").strip()
    if not exact_turn_id:
        _required_tui_runtime(params).set_notice(
            "Turn is still binding · stop was not sent",
            duration_seconds=2.5,
        )
        event.app.invalidate()
        return
    _required_tui_runtime(params).request_interrupt()
    _tui_submit_control_operation(params, "/stop")
    event.app.invalidate()


# LLM: Down 必须先按当前渲染宽度走软折视觉行；到视觉底边后才委托 Buffer 处理下一逻辑行或较新 history。
# 函数用途: 在屏幕折出的第 2、3 行间向下移动，到末行后再回到较新的历史或原草稿。
def _handle_down_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    buffer = params.input_area.buffer
    if buffer.complete_state is not None:
        move_completion_selection(buffer, event.arg)
        event.app.invalidate()
        return
    if move_input_cursor_by_wrapped_rows(
        buffer,
        width=_input_visual_width(event, params),
        delta=max(1, int(event.arg)),
    ):
        _reset_exit_arms(params)
        event.app.invalidate()
        return
    buffer.auto_down(count=event.arg)


# LLM: Ctrl-P/N 与箭头共用 completion index；菜单关闭时退回 Buffer 历史/多行移动，不建立第二导航状态。
# 函数用途: 在补全菜单中循环选择，或按 Emacs 习惯浏览输入历史。
def _handle_completion_or_history(
    event,
    params: TuiCreateKeybindingsParams,
    direction: int,
) -> None:
    buffer = params.input_area.buffer
    if buffer.complete_state is not None:
        move_completion_selection(buffer, int(direction) * event.arg)
        event.app.invalidate()
        return
    if move_input_cursor_by_wrapped_rows(
        buffer,
        width=_input_visual_width(event, params),
        delta=int(direction) * max(1, int(event.arg)),
    ):
        event.app.invalidate()
        return
    if direction < 0:
        buffer.auto_up(count=event.arg, go_to_start_of_line_if_history_changes=True)
    else:
        buffer.auto_down(count=event.arg, go_to_start_of_line_if_history_changes=True)


# LLM: 宽度优先读取输入 Window 最近一次公开 render_info；未渲染测试/启动窗口才按 output 列数减去固定两列 prompt，不能读 transcript 宽度猜输入布局。
# 函数用途: 取得当前输入正文真正可用的折行宽度，供上下键做视觉行导航。
def _input_visual_width(event, params: TuiCreateKeybindingsParams) -> int:
    window = getattr(params.input_area, "window", None)
    render_info = getattr(window, "render_info", None)
    rendered_width = int(getattr(render_info, "window_width", 0) or 0)
    if rendered_width > 0:
        return rendered_width
    output = getattr(getattr(event, "app", None), "output", None)
    get_size = getattr(output, "get_size", None)
    if callable(get_size):
        try:
            columns = int(getattr(get_size(), "columns", 0) or 0)
        except (OSError, TypeError, ValueError):
            columns = 0
        if columns > 2:
            return columns - 2
    return 80


# LLM: Ctrl-L 只请求终端重绘，typed transcript/history 不被删除或另建空 projection。
# 函数用途: 重绘当前 TUI 画面。
def _handle_ctrl_l_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    del params
    event.app.invalidate()


# LLM: Ctrl-O 通过唯一 transcript state 冻结/释放当前 typed view snapshot，并显式切换焦点；不会改写消息正文或业务 verbose_level。
# 函数用途: 进入或退出 终端交互 风格的详细 transcript 模式。
def _handle_ctrl_o_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    state = _required_transcript_state(params)
    if state.snapshot().active:
        _exit_transcript_mode(event, params)
        return
    state.enter(_required_tui_runtime(params).store.snapshot())
    params.transcript_area.modal_control.move_end()
    event.app.layout.focus(params.transcript_area.modal_window)
    event.app.invalidate()


# LLM: chat 模式 Ctrl-E 保持 Emacs 行尾移动；transcript 的 show-all 由互斥 binding 单独处理。
# 函数用途: 把普通输入光标移动到当前逻辑行末。
def _handle_ctrl_e_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    del event
    buffer = params.input_area.buffer
    buffer.cursor_position += buffer.document.get_end_of_line_position()


# LLM: Ctrl-E 只切换 transcript state 的 show_all 字段；renderer 读取该结构化字段展开折叠块。
# 函数用途: 在详细 transcript 中显示全部或恢复折叠。
def _handle_transcript_show_all(event, params: TuiCreateKeybindingsParams) -> None:
    _required_transcript_state(params).toggle_show_all()
    event.app.invalidate()


# LLM: 退出动作统一销毁 frozen/search 状态、清搜索 Buffer 并把焦点还给 chat；q/Esc/Ctrl-C/Ctrl-O 必须共用此入口。
# 函数用途: 退出详细 transcript 回到普通输入界面。
def _exit_transcript_mode(event, params: TuiCreateKeybindingsParams) -> None:
    state = _required_transcript_state(params)
    state.exit()
    params.transcript_search_area.text = ""
    event.app.layout.focus(params.input_area)
    event.app.invalidate()


# LLM: `/` 只在 transcript navigation 模式打开搜索，并保存当前结构化行锚点；不会插入 chat 输入。
# 函数用途: 打开 transcript 增量搜索栏。
def _open_transcript_search(event, params: TuiCreateKeybindingsParams) -> None:
    params.transcript_search_area.text = ""
    state = _required_transcript_state(params)
    state.open_search(params.transcript_area.current_line())
    event.app.layout.focus(params.transcript_search_area)
    event.app.invalidate()


# LLM: Enter 在 provider 已用当前 query 计算 matches 后提交 search；关闭栏后保留可导航高亮并跳到当前命中。
# 函数用途: 接受 transcript 搜索词并返回导航模式。
def _commit_transcript_search(event, params: TuiCreateKeybindingsParams) -> None:
    params.transcript_area.provider.frame(params.transcript_area.provider.last_width)
    state = _required_transcript_state(params)
    line = state.commit_search()
    params.transcript_search_area.text = ""
    params.transcript_area.jump_search_match(line)
    event.app.layout.focus(params.transcript_area.modal_window)
    event.app.invalidate()


# LLM: 取消搜索必须恢复 `/` 前 committed query 与滚动锚点；清 query Buffer 发生在 state 关闭后，listener 不会重写状态。
# 函数用途: 取消 transcript 搜索并返回 navigation 模式。
def _cancel_transcript_search(event, params: TuiCreateKeybindingsParams) -> None:
    anchor = _required_transcript_state(params).cancel_search()
    params.transcript_search_area.text = ""
    params.transcript_area.modal_control.jump_to(anchor)
    event.app.layout.focus(params.transcript_area.modal_window)
    event.app.invalidate()


# LLM: n/N 只遍历 state 已从可见 frame 建立的 matches；无 committed query 时保持当前位置。
# 函数用途: 跳到下一个或上一个 transcript 搜索命中。
def _navigate_transcript_search(
    event,
    params: TuiCreateKeybindingsParams,
    *,
    reverse: bool,
) -> None:
    params.transcript_area.provider.frame(params.transcript_area.provider.last_width)
    line = _required_transcript_state(params).navigate(reverse=reverse)
    params.transcript_area.jump_search_match(line)
    event.app.invalidate()


def _handle_alt_r_keybinding(event, params: TuiCreateKeybindingsParams) -> None:
    """会话运行时 Alt+R(toggle raw output)映射为 /verbose 档位循环 off→on→full→off。

    读当前会话的持久 verbose_level(与 control_runtime._execute_local_verbose
    同一线程身份), 结构化算出下一档再发显式命令——不做自然语言解析。
    """
    current = "off"
    try:
        store = getattr(params.agent, "conversation_store", None)
        if store is not None:
            thread = store.get_or_create_thread(
                {
                    "canonical_user_id": "local-agent",
                    "channel": "chat",
                    "channel_conversation_id": params.current_session_id or "default",
                    "channel_user_id": "local-agent",
                    "title": "会话设置",
                }
            )
            current = str(getattr(thread, "verbose_level", "off") or "off").strip().lower()
    except Exception:  # noqa: BLE001 读不到按 off 起步, 不拦切换
        current = "off"
    next_level = {"off": "on", "on": "full", "full": "off"}.get(current, "on")
    _tui_handle_command(params=_handle_command_params(params, f"/verbose {next_level}"))


def _request_exit(params: TuiCreateKeybindingsParams) -> None:
    _tui_request_exit(
        TuiExitRefs(
            shutting_down_ref=params.shutting_down_ref,
            state_lock=params.state_lock,
            is_running_ref=params.is_running_ref,
            pending_jobs_ref=params.pending_jobs_ref,
            stop_event=params.stop_event,
        )
    )


def _set_transcript_follow(params: TuiCreateKeybindingsParams, value: bool) -> None:
    if params.transcript_follow_ref is not None:
        params.transcript_follow_ref[0] = value


# LLM: typed transcript view 提供 scroll API；legacy buffer 分支仅供独立旧测试对象，产品主链不会选择它。
# 函数用途: 移动 transcript 可视锚点。
def _move_transcript_cursor(area: Any, delta: int) -> None:
    if area is None:
        return
    if callable(getattr(area, "scroll", None)):
        area.scroll(delta)
        return
    if delta < 0:
        area.buffer.cursor_up(count=abs(delta))
    elif delta > 0:
        area.buffer.cursor_down(count=delta)


def _scroll_transcript(params: TuiCreateKeybindingsParams, delta: int) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, False)
    _move_transcript_cursor(params.transcript_area, delta)


def _scroll_transcript_home(params: TuiCreateKeybindingsParams) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, False)
    if callable(getattr(params.transcript_area, "home", None)):
        params.transcript_area.home()
        return
    params.transcript_area.buffer.cursor_position = 0


def _scroll_transcript_end(params: TuiCreateKeybindingsParams) -> None:
    if params.transcript_area is None:
        return
    _set_transcript_follow(params, True)
    if callable(getattr(params.transcript_area, "end", None)):
        params.transcript_area.end()
        return
    params.transcript_area.buffer.cursor_position = len(params.transcript_area.text)


# LLM: 双击退出只使用 monotonic 时间和独立 Ctrl-C/Ctrl-D ref；超时后第一次按键永远不退出。
# 函数用途: 记录退出按键时间并返回本次是否是有效第二击。
def _arm_exit(params: TuiCreateKeybindingsParams, *, eof: bool) -> bool:
    attr = "eof_armed_at_ref" if eof else "exit_armed_at_ref"
    ref = getattr(params, attr)
    if ref is None:
        return False
    now = time.monotonic()
    armed = bool(ref[0] and now - ref[0] <= 0.8)
    ref[0] = 0.0 if armed else now
    return armed


# LLM: 双 Esc 计时还绑定首击时的精确 buffer 文本，期间编辑会让下一次 Esc 重新成为首击。
# 函数用途: 记录清空草稿按键并判断是否为有效第二次 Esc。
def _arm_escape(params: TuiCreateKeybindingsParams, text: str) -> bool:
    time_ref = params.escape_armed_at_ref
    text_ref = params.escape_armed_text_ref
    if time_ref is None or text_ref is None:
        return False
    now = time.monotonic()
    armed = bool(
        time_ref[0]
        and now - time_ref[0] <= 0.8
        and text_ref[0] == text
    )
    time_ref[0] = 0.0 if armed else now
    text_ref[0] = "" if armed else text
    return armed


# LLM: escape arm 与退出 arm 分开复位，Esc 清空不应意外取消刚建立的 Ctrl-C/Ctrl-D 退出窗口。
# 函数用途: 清除双 Esc 的时间和草稿快照。
def _reset_escape_arm(params: TuiCreateKeybindingsParams) -> None:
    if params.escape_armed_at_ref is not None:
        params.escape_armed_at_ref[0] = 0.0
    if params.escape_armed_text_ref is not None:
        params.escape_armed_text_ref[0] = ""


# LLM: 任一确定输入动作都会解除两种退出 arm，避免迟到第二击误关会话。
# 函数用途: 清空 Ctrl-C/Ctrl-D 双击计时状态。
def _reset_exit_arms(params: TuiCreateKeybindingsParams) -> None:
    for ref in (params.exit_armed_at_ref, params.eof_armed_at_ref):
        if ref is not None:
            ref[0] = 0.0
    _reset_escape_arm(params)
