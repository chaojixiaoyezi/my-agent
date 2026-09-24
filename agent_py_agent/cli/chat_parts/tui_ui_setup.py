# LLM: 本模块只把 TuiRuntime 的 typed snapshot 接入 prompt_toolkit 布局；不得恢复字符串 transcript、前缀 lexer 或第二状态 store。
# 模块用途: 创建行内对话、权限、输入和底部提示；每个代理视图都显示当前客户端的真实刷新健康。

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from ...agent.common.opaque_id import validate_opaque_id
from .plugin_command_client import PluginCommandClient
from .rendering import set_tui_output_sink
from .tui_block_renderer import TuiRenderContext
from .tui_input import (
    TuiCompletionMenuControl,
    TuiHistoryAutoSuggest,
    TuiInputCompleter,
    TuiQueuedPlaceholderProcessor,
)
from .tui_interaction import TuiInteractionState
from .tui_keybindings import (
    TuiCreateKeybindingsParams,
    _selected_input_text,
    _tui_create_keybindings,
    _write_selection_clipboard,
)
from .tui_params import MakeTuiAppParams
from .tui_plugin_commands import bind_plugin_input
from .tui_plugin_panels import PluginPanelBoard, make_plugin_panel_window
from .tui_runtime import TuiRuntime
from .tui_terminal import TuiTerminalTitleController
from .tui_transcript import TuiTranscriptModeState
from .tui_view import (
    TuiTranscriptView,
    _right_copy_mouse_transition,
    make_tui_transcript_view,
)

# LLM: 仅合并可见帧，不延迟工具/消息账和按键处理；终端整屏布局需限制频率，避免流式 token 占满单核。
# 常量用途: 流式输出最多每秒绘制 20 帧，输入和滚动事件仍立即请求下一帧。
APP_REDRAW_INTERVAL_SECONDS = 1 / 20
APP_RENDER_POSTPONE_SECONDS = 1 / 20
ESCAPE_SEQUENCE_TIMEOUT_SECONDS = 0.1
TERMINAL_ESCAPE_PREFIX_TIMEOUT_SECONDS = 0.05


# LLM: 输入保留原 history 与补全；插件客户端只在显式 Tab/提交读取，候选原 revision 跟随输入框，不增加启动请求。
# 函数用途: 创建底部可增长到八行的聊天输入框。
def _make_input_area(
    history_file_path: str,
    runtime: TuiRuntime,
    workspace: Path,
    *, plugin_client: PluginCommandClient | None = None,
    plugin_panels: PluginPanelBoard | None = None,
) -> Any:
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import TextArea

    input_area = TextArea(
        height=Dimension(min=1, max=8),
        dont_extend_height=True,
        style="class:tui-input",
        multiline=True,
        wrap_lines=True,
        history=FileHistory(history_file_path),
        auto_suggest=TuiHistoryAutoSuggest(),
        completer=TuiInputCompleter(workspace, plugin_client=plugin_client, catalog_error=runtime.set_notice),
        complete_while_typing=True,
        input_processors=[TuiQueuedPlaceholderProcessor(runtime)],
    )
    if plugin_client is not None:
        bind_plugin_input(input_area.buffer, plugin_client, plugin_panels)
    return input_area


# LLM: 输入 marker 是纯显示前缀；用户提交后的 transcript 背景块由 reducer/renderer 独立生成。
# 函数用途: 创建 终端交互 风格的 `❯ ` 输入提示。
def _make_input_prompt_window() -> Any:
    from prompt_toolkit.layout import FormattedTextControl, Window

    return Window(
        content=FormattedTextControl([("class:tui-input-marker", "❯ ")]),
        width=2,
        dont_extend_width=True,
    )


# LLM: context factory 只读取公开配置、typed TUI status/block 和显示时钟；模型正文与业务对象不得成为动画事实源。
# 函数用途: 生成当前代理的渲染上下文；主/子页面共用当前客户端后台读取健康，切页不掩盖故障。
def _make_render_context_factory(
    params: MakeTuiAppParams,
    interaction: TuiInteractionState,
    transcript_state: TuiTranscriptModeState,
):
    agent_name = str(getattr(params.agent.config, "agent_name", "my-agent") or "my-agent")
    root = getattr(params.agent, "root", "")
    try:
        workspace = str(Path(root)) if root else ""
    except (TypeError, ValueError):
        workspace = ""
    runtime = _required_runtime(params)
    navigation = params.agent_navigation

    # LLM: 子页面内容来自其独立 runtime，连接健康只读取 root runtime，避免产生多份相互矛盾的健康状态。
    # 函数用途: 按页面和宽度读取展示数据；已确认模型名跟随实时选择，展开历史也不会冻结旧标签。
    def make_context(width: int) -> TuiRenderContext:
        interaction_snapshot = interaction.snapshot()
        transcript_snapshot = transcript_state.snapshot()
        active_runtime = (
            navigation.active_runtime()
            if callable(getattr(navigation, "active_runtime", None))
            else runtime
        )
        navigation_snapshot = (
            navigation.snapshot()
            if callable(getattr(navigation, "snapshot", None))
            else None
        )
        view_snapshot = active_runtime.store.snapshot()
        sync_snapshot = view_snapshot if active_runtime is runtime else runtime.store.snapshot()
        status = view_snapshot.status
        return TuiRenderContext(
            width=width,
            agent_name=agent_name,
            model_name=view_snapshot.selected_model_name or str(getattr(params.agent.config, "model_name", "") or ""),
            workspace=workspace,
            detailed_transcript=transcript_snapshot.active,
            show_all=transcript_snapshot.show_all,
            spinner_index=int(time.monotonic() * 4),
            now=time.time(),
            status_started_at=status.started_at,
            status_last_event_at=status.last_event_at,
            context_tokens=status.context_tokens,
            context_usage=status.context_usage,
            model_metrics=tuple(sorted(status.model_metrics.items())),
            compact_count=status.compact_count,
            output_tokens=status.output_tokens,
            has_active_tools=any(
                block.role == "tool"
                and block.phase not in {"completed", "failed", "interrupted"}
                for block in view_snapshot.active_blocks
            ),
            notice=active_runtime.notice(),
            background_sync_failed=sync_snapshot.background_sync_failed,
            has_stash=interaction_snapshot.has_stash,
            is_pasting=interaction_snapshot.is_pasting,
            help_open=interaction_snapshot.help_open,
            todos_expanded=interaction_snapshot.todos_expanded,
            mouse_capture_enabled=interaction_snapshot.mouse_capture_enabled,
            focused_agent_run_id=str(
                getattr(navigation_snapshot, "active_run_id", "") or ""
            ),
            focused_agent_name=str(
                getattr(navigation_snapshot, "active_name", "main") or "main"
            ),
            focused_agent_status=str(
                getattr(navigation_snapshot, "active_status", "") or ""
            ),
            selected_agent_run_id=str(
                getattr(navigation_snapshot, "selected_run_id", "") or ""
            ),
            expanded_goal_id=str(
                getattr(navigation_snapshot, "expanded_goal_id", "") or ""
            ),
            agent_view_depth=max(
                0,
                int(getattr(navigation_snapshot, "depth", 0) or 0),
            ),
        )

    return make_context


# LLM: overlay Window 的高度直接来自 typed frame 行数；零行时不占布局空间，也不根据文案猜权限状态。
# 函数用途: 创建动态高度的底部权限面板区域。
def _make_overlay_window(view: TuiTranscriptView) -> Any:
    from prompt_toolkit.layout import Window

    return Window(
        content=view.overlay_control,
        height=view.overlay_line_count,
        dont_extend_height=True,
    )


# LLM: 权限 feedback TextArea 只编辑 reducer 当前 permission 的普通说明；read_only 条件由显式 feedback_mode 控制。
# 函数用途: 创建 终端交互 `Tab to amend` 展开后的单行补充说明框。
def _make_permission_feedback_area(runtime: TuiRuntime) -> Any:
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.widgets import TextArea

    feedback_active = Condition(
        lambda: bool(
            (permission := runtime.store.snapshot().permission)
            and permission.feedback_mode
        )
    )

    def prompt() -> list[tuple[str, str]]:
        permission = runtime.store.snapshot().permission
        placeholder = (
            permission.feedback_placeholder
            if permission is not None
            else ""
        )
        return [("class:tui-muted", f"  › {placeholder}: " if placeholder else "  › ")]

    return TextArea(
        height=1,
        dont_extend_height=True,
        multiline=False,
        wrap_lines=False,
        read_only=~feedback_active,
        style="class:tui-permission-feedback",
        prompt=prompt,
    )


# LLM: listener 只在同一 permission 的 feedback_mode 中提交文本草稿；程序化投影和面板关闭不会生成事件。
# 函数用途: 连接权限补充说明输入与 typed TUI runtime。
def _wire_permission_feedback_area(area: Any, runtime: TuiRuntime) -> None:
    def on_feedback_changed(buffer: Any) -> None:
        permission = runtime.store.snapshot().permission
        text = str(buffer.text or "")
        if (
            permission is None
            or not permission.feedback_mode
            or permission.feedback == text
        ):
            return
        runtime.update_permission_feedback(permission.permission_id, text)

    area.buffer.on_text_changed += on_feedback_changed


# LLM: input status Window 只展示 typed pending steer、follow-up queue、context usage 和 interaction stash。
# LLM: 零行时不占高度，也不能改变 transcript anchor。
# 函数用途: 创建输入框上方的待插入消息、上下文与草稿状态区域。
def _make_input_status_window(view: TuiTranscriptView) -> Any:
    from prompt_toolkit.layout import Window

    return Window(
        content=view.input_status_control,
        height=lambda: len(
            view.provider.frame(view.provider.last_width).input_status_lines
        ),
        dont_extend_height=True,
    )


# LLM: Todo has its own fixed region above the composer, matching 终端交互's
# standalone TaskListV2 placement; it never enters scrollback or the input buffer.
# 函数用途: 创建输入框上方的固定任务清单区域，空清单不占高度。
def _make_todo_window(view: TuiTranscriptView) -> Any:
    from prompt_toolkit.layout import Window

    return Window(
        content=view.todo_control,
        height=lambda: len(view.provider.frame(view.provider.last_width).todo_lines),
        dont_extend_height=True,
    )


# LLM: The coordinator panel is a separate fixed region below the prompt footer
# for exact Goal and child rows. Main activity follows 终端交互's
# SpinnerWithVerb at the transcript tail and must not be duplicated here.
# 函数用途: 创建输入框下方的 Goal/直属子代理状态区域；两者都不存在时原位收起。
def _make_agent_window(view: TuiTranscriptView) -> Any:
    from prompt_toolkit.layout import Window

    return Window(
        content=view.agent_control,
        height=lambda: len(view.provider.frame(view.provider.last_width).agent_lines),
        dont_extend_height=True,
    )


# LLM: 历史搜索使用独立单行 Buffer；主输入只显示当前 match，query 永远不会混入待提交 prompt。
# 函数用途: 创建带动态 `search prompts:` 前缀的 Ctrl-R 搜索底栏。
def _make_history_search_area(interaction: TuiInteractionState) -> Any:
    from prompt_toolkit.widgets import TextArea

    return TextArea(
        height=1,
        dont_extend_height=True,
        multiline=False,
        wrap_lines=False,
        style="class:tui-history-search",
        prompt=lambda: [
            (
                "class:tui-muted",
                "  "
                + (
                    "no matching prompt: "
                    if interaction.snapshot().history_failed_match
                    else "search prompts: "
                ),
            )
        ],
    )


# LLM: query listener 只在 active search 中把结构化匹配结果投影到主 Buffer；程序化清空搜索框时不会改草稿。
# 函数用途: 连接搜索输入变化与主输入中的历史匹配预览。
def _wire_history_search_area(
    history_search_area: Any,
    input_area: Any,
    interaction: TuiInteractionState,
) -> None:
    from prompt_toolkit.document import Document

    def on_query_changed(buffer: Any) -> None:
        if not interaction.snapshot().history_search_active:
            return
        draft = interaction.update_history_query(str(buffer.text or ""))
        if draft is None:
            return
        input_area.buffer.set_document(
            Document(draft.text, cursor_position=draft.cursor_position),
            bypass_readonly=True,
        )

    history_search_area.buffer.on_text_changed += on_query_changed


# LLM: `?` 本身不进入 Buffer；一旦用户真正输入其它正文，帮助必须从同一 interaction state 幂等关闭。
# 函数用途: 让普通文字输入自动收起快捷键帮助，同时保留刚输入的字符和光标位置。
def _wire_help_dismiss_on_input(
    input_area: Any,
    interaction: TuiInteractionState,
    agent_navigation: object | None = None,
) -> None:
    def on_input_changed(buffer: Any) -> None:
        if str(buffer.text or ""):
            interaction.close_help()
            clear_selection = getattr(agent_navigation, "clear_selection", None)
            if callable(clear_selection):
                clear_selection()

    input_area.buffer.on_text_changed += on_input_changed


# LLM: transcript 搜索使用独立一行 Buffer；正文全文与命中坐标由 TuiTranscriptModeState/provider 管理，query 不进入 chat history。
# 函数用途: 创建 less 风格 `/` transcript 搜索输入框。
def _make_transcript_search_area() -> Any:
    from prompt_toolkit.widgets import TextArea

    return TextArea(
        height=1,
        dont_extend_height=True,
        multiline=False,
        wrap_lines=False,
        style="class:tui-transcript-search",
        prompt=[("class:tui-muted", "  /")],
    )


# LLM: query listener 只更新 transcript typed state，再让 provider 按当前宽度计算可见 match；它不直接扫描 block metadata。
# 函数用途: 连接 transcript 搜索框与高亮/跳转。
def _wire_transcript_search_area(
    search_area: Any,
    view: TuiTranscriptView,
    transcript_state: TuiTranscriptModeState,
) -> None:
    def on_query_changed(buffer: Any) -> None:
        if not transcript_state.snapshot().search_open:
            return
        transcript_state.update_search_query(str(buffer.text or ""))
        view.provider.frame(view.provider.last_width)
        view.jump_search_match(transcript_state.current_match_line())

    search_area.buffer.on_text_changed += on_query_changed


# LLM: 搜索状态行只显示 state 已计算的 count/current；零匹配错误不是控制信号。
# 函数用途: 创建 transcript 搜索栏右侧的命中计数或无结果提示。
def _make_transcript_search_status_window(
    transcript_state: TuiTranscriptModeState,
) -> Any:
    from prompt_toolkit.layout import FormattedTextControl, Window
    from prompt_toolkit.layout.dimension import Dimension

    def text() -> list[tuple[str, str]]:
        snapshot = transcript_state.snapshot()
        if snapshot.search_query and snapshot.match_count == 0:
            return [("class:tui-error", "no matches ")]
        if snapshot.match_count:
            return [
                (
                    "class:tui-muted",
                    f"{snapshot.current_match}/{snapshot.match_count}  ",
                )
            ]
        return []

    return Window(
        content=FormattedTextControl(text),
        width=Dimension(min=0, max=20),
        height=1,
        dont_extend_height=True,
    )


# LLM: footer 与 transcript 共享 frame；普通状态固定一行，显式 help 状态按其真实换行数占高。
# 函数用途: 创建快捷键、运行状态和 `?` 帮助区域。
def _make_footer_window(view: TuiTranscriptView) -> Any:
    from prompt_toolkit.layout import Window

    return Window(
        content=view.footer_control,
        height=view.footer_line_count,
        dont_extend_height=True,
        style="class:tui-footer",
    )


# LLM: 输入上下横线是固定视觉组件，不承载 mode/permission 状态，也不从终端内容测算长度。
# 函数用途: 创建随当前可用宽度铺满的输入分隔线。
def _make_input_rule_window() -> Any:
    from prompt_toolkit.layout import Window

    return Window(
        height=1,
        char="─",
        style="class:tui-input-rule",
        dont_extend_height=True,
    )


# LLM: transcript 与输入框之间保留 终端交互 的单空行，不能并入消息 block 或会话历史。
# 函数用途: 创建一行纯布局间距。
def _make_input_spacer() -> Any:
    from prompt_toolkit.layout import Window

    return Window(height=1, dont_extend_height=True)


# LLM: runtime 是 mandatory dependency；缺失时必须显式失败，禁止回退创建 legacy transcript store。
# 函数用途: 校验并返回 app 与 worker 共享的 TuiRuntime。
def _required_runtime(params: MakeTuiAppParams) -> TuiRuntime:
    runtime = params.tui_runtime
    if not isinstance(runtime, TuiRuntime):
        raise TypeError("MakeTuiAppParams.tui_runtime must be TuiRuntime")
    return runtime


# LLM: 两个超时分别约束终端 ESC 前缀拆包和 prompt_toolkit 多键歧义；必须一起收紧并保留短窗识别 Alt/Meta 组合键。
# 函数用途: 配置单按 Esc 的低延迟，同时继续识别同批送达的 Alt+Enter、Alt+R 等组合键。
def _configure_escape_timeouts(application: Any) -> None:
    application.ttimeoutlen = TERMINAL_ESCAPE_PREFIX_TIMEOUT_SECONDS
    application.timeoutlen = ESCAPE_SEQUENCE_TIMEOUT_SECONDS


# LLM: FileHistory belongs to the canonical owner-scoped session workspace, never the
# project workspace; keep the validated session ID as the only per-session path segment.
# 函数用途: 返回当前 TUI 会话的输入历史文件，避免用户刚输入的提示词污染项目搜索和产物目录。
def _tui_input_history_path(params: MakeTuiAppParams) -> Path:
    session_id = validate_opaque_id(
        str(params.current_session_id or "default").strip() or "default",
        kind="session_id",
    )
    session_root = str(
        getattr(getattr(params.agent, "config", None), "session_workspace", "")
        or ""
    ).strip()
    if not session_root:
        raise RuntimeError("TUI 输入历史缺少 owner-scoped session_workspace")
    return Path(session_root).expanduser() / session_id / "input_history"


# LLM: 组件束只保存同一 Application 的 typed runtime、state、controls 和 filters；不得被用作第二状态源或跨 app 复用。
# 类用途: 汇总界面组装和按键注册共同使用的控件与模式条件。
@dataclass(frozen=True)
class _TuiAppParts:
    runtime: TuiRuntime
    interaction: TuiInteractionState
    transcript_state: TuiTranscriptModeState
    title_controller: TuiTerminalTitleController
    input_area: Any
    history_search_area: Any
    transcript_view: TuiTranscriptView
    transcript_search_area: Any
    permission_feedback_area: Any
    history_search_active: Any
    permission_active: Any
    permission_feedback_active: Any
    transcript_search_active: Any
    plugin_panels: PluginPanelBoard | None = None


# LLM: 控件准备只创建原 runtime 视图和易失插件客户端；目录不在此读取，history 目录仍是唯一文件系统副作用。
# 函数用途: 创建 TUI 所需的状态、控件和动态模式条件。
def _prepare_tui_app_parts(params: MakeTuiAppParams) -> _TuiAppParts:
    from prompt_toolkit.filters import Condition

    runtime = _required_runtime(params)
    interaction = TuiInteractionState(
        runtime.store.invalidate,
        mouse_capture_enabled=bool(
            getattr(params.agent.config, "tui_mouse_capture_default", True)
        ),
    )
    transcript_state = TuiTranscriptModeState(runtime.store.invalidate)
    title_controller = TuiTerminalTitleController(
        str(getattr(params.agent.config, "agent_name", "my-agent") or "my-agent")
    )
    history_file = _tui_input_history_path(params)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    plugin_client = PluginCommandClient(params.agent, str(params.current_session_id or "default"), use_gateway=params.use_gateway)
    # 面板只在用户打开时才启动后台刷新；TUI 退出事件会让刷新线程结束
    plugin_panels = PluginPanelBoard(plugin_client.panels, runtime.store.invalidate, stop_event=params.stop_event)
    input_area = _make_input_area(str(history_file), runtime, Path(params.agent.root), plugin_client=plugin_client,
                                  plugin_panels=plugin_panels)
    _wire_help_dismiss_on_input(input_area, interaction, params.agent_navigation)
    history_search_area = _make_history_search_area(interaction)
    _wire_history_search_area(history_search_area, input_area, interaction)
    history_search_active = Condition(
        lambda: interaction.snapshot().history_search_active
    )
    permission_active = Condition(
        lambda: runtime.store.snapshot().permission is not None
    )
    permission_feedback_active = Condition(
        lambda: bool(
            (permission := runtime.store.snapshot().permission)
            and permission.feedback_mode
        )
    )
    transcript_view = make_tui_transcript_view(
        runtime.store,
        _make_render_context_factory(params, interaction, transcript_state),
        transcript_state=transcript_state,
    )
    set_view_change = getattr(params.agent_navigation, "set_view_change_callback", None)
    if callable(set_view_change):
        view_depth_ref = [0]

        # LLM: A navigation switch delegates atomic store/viewport restoration to
        # the transcript view. A never-seen child opens at its delegated prompt;
        # returning restores its saved viewport and may surface one native-scroll hint.
        # 函数用途: 首次进入子代理从任务提示词开始看，返回或重进时恢复各页面原滚动位置。
        def switch_agent_view(selected_runtime: TuiRuntime) -> None:
            snapshotter = getattr(params.agent_navigation, "snapshot", None)
            navigation_snapshot = snapshotter() if callable(snapshotter) else None
            next_depth = max(
                0,
                int(getattr(navigation_snapshot, "depth", 0) or 0),
            )
            returning = next_depth < view_depth_ref[0]
            entering = next_depth > view_depth_ref[0]
            view_depth_ref[0] = next_depth
            transcript_view.set_state_store(
                selected_runtime.store,
                start_at_top_if_new=entering,
            )
            if (
                returning
                and not interaction.snapshot().mouse_capture_enabled
                and not selected_runtime.notice()
            ):
                selected_runtime.set_notice(
                    "历史已保留：PgUp/Ctrl+Home 翻阅；F6 恢复滚轮",
                    duration_seconds=5.5,
                )

        set_view_change(switch_agent_view)
    transcript_search_area = _make_transcript_search_area()
    _wire_transcript_search_area(
        transcript_search_area,
        transcript_view,
        transcript_state,
    )
    permission_feedback_area = _make_permission_feedback_area(runtime)
    _wire_permission_feedback_area(permission_feedback_area, runtime)
    transcript_search_active = Condition(
        lambda: transcript_state.snapshot().search_open
    )
    return _TuiAppParts(
        runtime=runtime,
        interaction=interaction,
        transcript_state=transcript_state,
        title_controller=title_controller,
        input_area=input_area,
        history_search_area=history_search_area,
        transcript_view=transcript_view,
        transcript_search_area=transcript_search_area,
        permission_feedback_area=permission_feedback_area,
        history_search_active=history_search_active,
        permission_active=permission_active,
        permission_feedback_active=permission_feedback_active,
        transcript_search_active=transcript_search_active,
        plugin_panels=plugin_panels,
    )


# LLM: normal body 的条件只引用 parts 中共享 filters；permission 覆盖层出现时必须隐藏输入、补全、历史、插件面板与 footer。
# 函数用途: 组装普通聊天和权限确认共用的主布局；插件面板区域位于输入状态栏上方。
def _make_normal_tui_body(parts: _TuiAppParts) -> Any:
    from prompt_toolkit.filters import Condition, has_completions
    from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit, Window
    from prompt_toolkit.layout.dimension import Dimension

    input_area = parts.input_area
    transcript_view = parts.transcript_view
    permission_active = parts.permission_active
    input_row = VSplit([_make_input_prompt_window(), input_area])
    panels = parts.plugin_panels
    return HSplit(
        [
            transcript_view.window,
            _make_overlay_window(transcript_view),
            ConditionalContainer(
                content=parts.permission_feedback_area,
                filter=parts.permission_feedback_active,
            ),
            # 插件面板只在有面板打开且没有审批时显示，高度有上限，不挤占输入区
            ConditionalContainer(
                content=make_plugin_panel_window(panels) if panels is not None else Window(height=0),
                filter=Condition(lambda: panels is not None and panels.has_visible()) & ~permission_active,
            ),
            ConditionalContainer(
                content=_make_input_status_window(transcript_view),
                filter=~permission_active,
            ),
            ConditionalContainer(
                content=_make_todo_window(transcript_view),
                filter=~permission_active,
            ),
            ConditionalContainer(
                content=_make_input_spacer(),
                filter=~permission_active,
            ),
            ConditionalContainer(
                content=_make_input_rule_window(),
                filter=~permission_active,
            ),
            ConditionalContainer(
                content=input_row,
                filter=~permission_active,
            ),
            ConditionalContainer(
                content=_make_input_rule_window(),
                filter=~permission_active,
            ),
            ConditionalContainer(
                content=Window(
                    content=TuiCompletionMenuControl(input_area.buffer),
                    height=Dimension(min=1, max=6),
                    dont_extend_height=True,
                ),
                filter=(
                    has_completions
                    & ~parts.history_search_active
                    & ~permission_active
                ),
            ),
            ConditionalContainer(
                content=parts.history_search_area,
                filter=parts.history_search_active & ~permission_active,
            ),
            ConditionalContainer(
                content=_make_footer_window(transcript_view),
                filter=(
                    ~permission_active
                    & ~has_completions
                    & ~parts.history_search_active
                ),
            ),
            ConditionalContainer(
                content=_make_agent_window(transcript_view),
                filter=(
                    ~permission_active
                    & ~has_completions
                    & ~parts.history_search_active
                ),
            ),
        ]
    )


# LLM: transcript modal body 始终复用同一个 view/state；搜索关闭时只替换底栏，不创建或复制正文控件。
# 函数用途: 组装详细 transcript 浏览和搜索布局。
def _make_transcript_tui_body(parts: _TuiAppParts) -> Any:
    from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit

    transcript_view = parts.transcript_view
    return HSplit(
        [
            transcript_view.modal_window,
            _make_input_rule_window(),
            ConditionalContainer(
                content=VSplit(
                    [
                        parts.transcript_search_area,
                        _make_transcript_search_status_window(parts.transcript_state),
                    ]
                ),
                filter=parts.transcript_search_active,
            ),
            ConditionalContainer(
                content=_make_footer_window(transcript_view),
                filter=~parts.transcript_search_active,
            ),
        ]
    )


# LLM: Application 组装必须保留同一个 parts 生命周期、alternate screen、显式块状输入光标、20Hz 合并上限、事件/活动动画触发重绘、动态鼠标 filter 与 before_render 焦点同步；不得另建 runtime。
# 函数用途: 用准备好的控件和两个模式布局创建可在 会话运行时 原生鼠标与 终端交互 TUI 鼠标间切换的全屏 Application。
def _assemble_tui_application(
    params: MakeTuiAppParams,
    parts: _TuiAppParts,
    normal_body: Any,
    transcript_body: Any,
) -> Any:
    from prompt_toolkit.application import Application
    from prompt_toolkit.cursor_shapes import CursorShape
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import ConditionalKeyBindings
    from prompt_toolkit.layout import DynamicContainer, FloatContainer, Layout

    body = DynamicContainer(
        lambda: (
            normal_body
            if parts.runtime.store.snapshot().permission is not None
            else transcript_body
            if parts.transcript_state.snapshot().active
            else normal_body
        )
    )
    model_host = FloatContainer(content=body, floats=[])
    layout = Layout(model_host, focused_element=parts.input_area)
    key_bindings = ConditionalKeyBindings(_make_tui_keybindings(params, parts),
        Condition(lambda: not getattr(app, "_my_agent_model_menu_active", False)))
    app = Application(
        layout=layout,
        key_bindings=key_bindings,
        style=_make_tui_style(),
        full_screen=True,
        erase_when_done=False,
        mouse_support=Condition(
            lambda: parts.interaction.snapshot().mouse_capture_enabled
        ),
        cursor=CursorShape.BLOCK,
        min_redraw_interval=APP_REDRAW_INTERVAL_SECONDS,
        max_render_postpone_time=APP_RENDER_POSTPONE_SECONDS,
        before_render=lambda application: None if getattr(application, "_my_agent_model_menu_active", False) else _before_tui_render(
            application,
            parts.input_area,
            parts.transcript_view,
            parts.transcript_search_area,
            parts.permission_feedback_area,
            parts.title_controller,
            parts.runtime,
        ),
    )
    app._my_agent_model_float_container = model_host
    app._my_agent_model_menu_active = False
    _configure_escape_timeouts(app)
    parts.transcript_view.provider.set_invalidate_callback(app.invalidate)
    parts.transcript_view.overlay_provider.set_invalidate_callback(app.invalidate)

    # LLM: 默认 TUI 鼠标模式同时支持滚轮与选区复制；回执绑定发起页面，由有序 projector 根据实际通道结果给出。
    # 函数用途: 松手或右键后异步复制最终选区；不在系统复制完成前声称成功，也不因随后切换子代理把回执发错页面。
    def copy_settled_selection(text: str) -> None:
        active_runtime = parts.runtime
        runtime_reader = getattr(params.agent_navigation, "active_runtime", None)
        if callable(runtime_reader):
            active_runtime = runtime_reader()
        _write_selection_clipboard(app, text, notify=active_runtime.set_notice)
        app.invalidate()

    parts.transcript_view.set_copy_on_select(copy_settled_selection)
    from .tui_history import wire_tui_history_pager

    wire_tui_history_pager(app, params, parts.transcript_view)
    from .tui_display_archive import wire_tui_display_archive
    wire_tui_display_archive(app, params, parts.transcript_view)
    _install_input_copy_on_select(parts.input_area, copy_settled_selection)
    if not parts.interaction.snapshot().mouse_capture_enabled:
        parts.runtime.set_notice(
            "原生复制模式：终端拖选/右键；历史用 PgUp/Ctrl+Home，F6 恢复滚轮",
            duration_seconds=6.0,
        )
    app._my_agent_title_controller = parts.title_controller
    def write_active_console(text: str) -> None:
        runtime_reader = getattr(params.agent_navigation, "active_runtime", None)
        selected_runtime = runtime_reader() if callable(runtime_reader) else parts.runtime
        selected_runtime.write_console(text)

    set_tui_output_sink(write_active_console)
    return app


# LLM: BufferControl 仍负责源字符坐标、焦点和 selection_state；wrapper 补齐包含式 focus，并在原 handler 之前拦截右键以保护选区。
# 函数用途: 给输入框增加拖选松手自动复制和选中后右键直接复制，两者都保留高亮。
def _install_input_copy_on_select(input_area: Any, copy_callback: Any) -> None:
    control = input_area.control
    state = _InputMouseCopyState(original_mouse_handler=control.mouse_handler)

    # LLM: wrapper 只把 prompt_toolkit 事件交给单一 state helper，不能自行复制第二套 gesture 状态机。
    # 函数用途: 保留 BufferControl 原有回调签名，并将事件交给输入选区复制控制器。
    def mouse_handler(mouse_event: Any):
        return _handle_input_copy_mouse_event(
            input_area,
            copy_callback,
            state,
            mouse_event,
        )

    control.mouse_handler = mouse_handler


# LLM: 输入 gesture state 只活在当前 TextArea control 生命周期；不得写入 transcript、session 或业务状态。
# 类用途: 保存一次左键拖选和右键复制的短暂坐标/latch，并保留原 prompt_toolkit handler。
@dataclass
class _InputMouseCopyState:
    original_mouse_handler: Any
    selection_anchor: int | None = None
    selection_moved: bool = False
    right_copy_armed: bool = False


# LLM: 输入事件必须先处理右键 latch，再调用原 handler 更新 Buffer 源字符坐标；复制后不清 selection_state。
# 函数用途: 推进一步输入鼠标 gesture，并返回原 BufferControl 的事件处理结果。
def _handle_input_copy_mouse_event(
    input_area: Any,
    copy_callback: Any,
    state: _InputMouseCopyState,
    mouse_event: Any,
) -> Any:
    buffer = input_area.buffer
    state.right_copy_armed, consume_event = _handle_input_right_copy(
        mouse_event,
        state.right_copy_armed,
        buffer,
        copy_callback,
    )
    if consume_event:
        return None
    result = state.original_mouse_handler(mouse_event)
    if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
        state.selection_anchor = int(buffer.cursor_position)
        state.selection_moved = False
    elif mouse_event.event_type == MouseEventType.MOUSE_MOVE:
        state.selection_moved = bool(
            state.selection_moved
            or (
                state.selection_anchor is not None
                and int(buffer.cursor_position) != state.selection_anchor
            )
        )
    if mouse_event.event_type == MouseEventType.MOUSE_UP:
        _finish_input_mouse_selection(buffer, state, copy_callback)
    return result


# LLM: 左键 release 只按显式 anchor/selection 完成包含式 focus 与复制；不得处理右键或清除 Buffer 高亮。
# 函数用途: 收口输入框左键拖选，复制最终正文并重置本轮拖动状态。
def _finish_input_mouse_selection(
    buffer: Any,
    state: _InputMouseCopyState,
    copy_callback: Any,
) -> None:
    anchor = state.selection_anchor
    if (
        anchor is not None
        and getattr(buffer, "selection_state", None) is not None
        and (state.selection_moved or int(buffer.cursor_position) != anchor)
    ):
        _include_input_focus_character(buffer, anchor)
    selected_text = _selected_input_text(buffer)
    if selected_text:
        copy_callback(selected_text)
    state.selection_anchor = None
    state.selection_moved = False


# LLM: 输入右键 helper 只复用显式 Buffer selection 和共享 gesture transition，不调用会修改光标的原 handler。
# 函数用途: 在输入框已有选区时执行一次右键复制，并返回下一 latch 与是否吞掉事件。
def _handle_input_right_copy(
    mouse_event: Any,
    armed: bool,
    buffer: Any,
    copy_callback: Any,
) -> tuple[bool, bool]:
    copy_requested, consume_event, next_armed = _right_copy_mouse_transition(
        mouse_event,
        armed,
    )
    if copy_requested:
        selected_text = _selected_input_text(buffer)
        if selected_text:
            copy_callback(selected_text)
    return next_armed, consume_event


# LLM: prompt_toolkit 的字符选区默认排除 forward drag 的 cursor 端；这里只对真实拖动补一个源字符，不改双击单词选择。
# 函数用途: 让输入拖选与 transcript 一样包含鼠标松手所在的最后一个字符。
def _include_input_focus_character(buffer: Any, anchor: int) -> None:
    selection = getattr(buffer, "selection_state", None)
    if selection is None:
        return
    text_length = len(str(buffer.text or ""))
    focus = int(buffer.cursor_position)
    normalized_anchor = max(0, min(text_length, int(anchor)))
    if focus >= normalized_anchor and focus < text_length:
        buffer.cursor_position = focus + 1
    elif focus < normalized_anchor and normalized_anchor < text_length:
        selection.original_cursor_position = normalized_anchor + 1


# LLM: make_tui_app 只组合一个 typed view/store；stdout 命令结果经 runtime.write_console 转成 system event，流式正文由 worker adapter 直发。
# 函数用途: 构建并返回完整 prompt_toolkit Application。
def make_tui_app(params: MakeTuiAppParams):
    parts = _prepare_tui_app_parts(params)
    return _assemble_tui_application(
        params,
        parts,
        _make_normal_tui_body(parts),
        _make_transcript_tui_body(parts),
    )


# LLM: keybinding 继承同一 typed view/runtime/local_run_ref；界面不查询或猜测资源执行身份。
# 函数用途: 把 app 状态组装为输入控制器参数。
def _make_tui_keybindings(
    app_config: MakeTuiAppParams,
    parts: _TuiAppParts,
):
    return _tui_create_keybindings(
        TuiCreateKeybindingsParams(
            parts.input_area,
            parts.transcript_view,
            None,
            app_config.agent,
            app_config.args,
            app_config.runtime_inject,
            app_config.prompt_files,
            app_config.use_gateway,
            app_config.paths,
            app_config.state_lock,
            app_config.is_running_ref,
            app_config.pending_jobs_ref,
            app_config.running_prompt_ref,
            app_config.running_request_id_ref,
            app_config.running_started_at_ref,
            app_config.shutting_down_ref,
            app_config.stop_event,
            app_config.assistant_outputs,
            app_config.jobs,
            app_config.pending_jobs_ref_for_enqueue,
            app_config.current_session_id,
            parts.interaction,
            parts.history_search_area,
            parts.transcript_state,
            parts.transcript_search_area,
            parts.permission_feedback_area,
            int(getattr(app_config.agent.config, "chat_transcript_scroll_lines", 10) or 10),
            app_config.tui_runtime,
            agent_navigation=app_config.agent_navigation,
            local_run_ref=app_config.local_run_ref,
        )
    )


# LLM: resize 可由 transcript state 在 render 时关闭搜索；此同步只修正焦点归属，不读取 query 文案或触发模式变化。
# 函数用途: 在每帧前保证 chat、transcript 和 transcript-search 的焦点与 typed mode 一致。
def _sync_transcript_focus(
    application: Any,
    input_area: Any,
    transcript_view: TuiTranscriptView,
    transcript_search_area: Any,
) -> None:
    snapshot = transcript_view.transcript_state.snapshot()
    current = application.layout.current_control
    if snapshot.active:
        if snapshot.search_open:
            if current is not transcript_search_area.control:
                application.layout.focus(transcript_search_area)
        elif current is transcript_search_area.control or current is input_area.control:
            application.layout.focus(transcript_view.modal_window)
        return
    if current is transcript_search_area.control or current is transcript_view.modal_control:
        application.layout.focus(input_area)


# LLM: permission overlay 比 transcript/history/chat 焦点优先；只读状态也聚焦专用控件，避免按键落入隐藏主输入。
# 函数用途: 在审批出现时接管焦点，并在关闭后把临时控件归还给普通界面。
def _sync_permission_focus(
    application: Any,
    input_area: Any,
    permission_feedback_area: Any,
    runtime: TuiRuntime,
) -> bool:
    from prompt_toolkit.document import Document

    permission = runtime.store.snapshot().permission
    current = application.layout.current_control
    if permission is None:
        if current is permission_feedback_area.control:
            application.layout.focus(input_area)
        return False
    if str(permission_feedback_area.text or "") != permission.feedback:
        permission_feedback_area.buffer.set_document(
            Document(permission.feedback, cursor_position=len(permission.feedback)),
            bypass_readonly=True,
        )
    if current is not permission_feedback_area.control:
        application.layout.focus(permission_feedback_area)
    return True


# LLM: before-render 只同步焦点并更新公开终端标题；两者都读取 typed state，不触发业务事件或模型调用。
# 函数用途: 执行每帧 TUI 的轻量终端副作用。
def _before_tui_render(
    application: Any,
    input_area: Any,
    transcript_view: TuiTranscriptView,
    transcript_search_area: Any,
    permission_feedback_area: Any,
    title_controller: TuiTerminalTitleController,
    runtime: TuiRuntime,
) -> None:
    permission_focused = _sync_permission_focus(
        application,
        input_area,
        permission_feedback_area,
        runtime,
    )
    if not permission_focused:
        _sync_transcript_focus(
            application,
            input_area,
            transcript_view,
            transcript_search_area,
        )
    title_controller.update(application.output, runtime.store.snapshot())


# LLM: style role 名必须与 block/Markdown renderer 一一对应；颜色是显示主题，不参与状态判断。
# 函数用途: 定义 终端交互 参考主题的 prompt_toolkit 样式表。
def _make_tui_style():
    from prompt_toolkit.styles import Style

    return Style.from_dict(
        {
            "tui-transcript": "",
            "tui-input": "#ffffff",
            "tui-input-marker": "#ffffff bold",
            "tui-placeholder": "#949494",
            "tui-input-rule": "#808080",
            "tui-tip-accent": "#d78787",
            "tui-footer": "#949494",
            "tui-new-messages": "#949494 bg:#3a3a3a",
            "tui-selection": "#ffffff bg:#5f5f87",
            "tui-user-marker": "#4e4e4e bg:#3a3a3a",
            "tui-user-text": "#ffffff bg:#3a3a3a bold",
            "tui-user-fill": "bg:#3a3a3a",
            "tui-assistant-marker": "#ffffff",
            "tui-accent": "#d7d7af",
            "tui-muted": "#949494",
            "tui-strong": "#ffffff bold",
            "tui-heading": "#ffffff bold",
            "tui-em": "italic",
            "tui-strike": "strike",
            "tui-link": "#afd7ff underline",
            "tui-code-inline": "#afd7ff",
            "tui-code-keyword": "#5fd7ff bold",
            "tui-code-string": "#ff5f5f",
            "tui-code-number": "#d7d7af",
            "tui-code-comment": "#949494 italic",
            "tui-code-builtin": "#5fd7ff",
            "tui-code-operator": "#ffffff",
            "tui-code-punctuation": "#949494",
            "tui-diff-add": "#d7efff bg:#33465c",
            "tui-diff-remove": "#ffd7d7 bg:#5c3338",
            "tui-diff-header": "#87afff bold",
            "tui-diff-context": "#d0d0d0",
            "tui-quote-mark": "#808080 italic",
            "tui-spinner": "#d78787",
            "tui-spinner-highlight": "#ffaf87",
            "tui-thinking": "#ffffff dim",
            "tui-thinking-detail": "#ffffff dim",
            "tui-subagent-running": "#87afff",
            "tui-subagent-pending": "#949494",
            "tui-subagent-done": "#87d787",
            "tui-subagent-blocked": "#d7af5f",
            "tui-agent-selected": "#ffffff bg:#3a3a3a bold",
            "tui-context-label": "#6c6c6c",
            "tui-context-safe": "#87afaf",
            "tui-context-warning": "#d7af5f",
            "tui-context-danger": "#ff8787 bold",
            "tui-tool-marker": "#ffffff",
            "tui-tool-title": "#ffffff",
            "tui-tool-output": "#c6c6c6",
            "tui-tool-output-error": "#ff8787",
            "tui-error": "#ff5f5f",
            "tui-welcome-heading": "#d7d7af bold",
            "tui-welcome-divider": "#d7d7af dim",
            "tui-avatar-hair": "#ffd7af",
            "tui-avatar-ribbon": "#ff8787 bold",
            "tui-avatar-face": "#ffd7d7 bold",
            "tui-avatar-dress": "#ff875f bold",
            "tui-avatar-umbrella": "#ff5f5f bold",
            "tui-avatar-bunny": "#ffd7d7",
            "tui-permission-accent": "#d7d7af",
            "tui-permission-title": "#ffffff bold",
            "tui-permission-selected": "#ffffff bg:#3a3a3a",
            "tui-permission-feedback": "#ffffff",
            "tui-completion": "#949494 bg:default",
            "tui-completion-selected": "#afd7ff bg:default",
            "tui-history-search": "#949494",
            "tui-transcript-search": "#ffffff",
            "tui-search-match": "bg:#5f5f00",
            "tui-search-current": "#000000 bg:#ffff5f bold",
        }
    )


__all__ = ["make_tui_app"]
