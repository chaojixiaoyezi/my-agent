# LLM: 本模块把 typed snapshot renderer 接入 prompt_toolkit UIControl；只管理画面缓存、cursor/follow 和 redraw，不持有业务执行权。
# 模块用途: 提供可滚动 transcript control、固定 overlay/footer 内容和稳定 block cache，替代字符串 TextArea+行前缀 lexer。

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from .tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    TuiRenderFrame,
    render_tui_snapshot,
    sanitize_tui_render_frame,
    tui_render_context_key,
)
from .tui_markdown import FormattedLine, fragments_text
from .tui_transcript import TuiTranscriptModeState
from .tui_view_model import TuiStateStore, TuiViewSnapshot

DEFAULT_TRANSCRIPT_WIDTH = 80


# LLM: 选区坐标只属于当前宽度下的可见 transcript；resize 会清除，不能持久化或成为会话正文引用。
# 类用途: 保存鼠标拖选的起点和终点，供高亮与 Ctrl-C 复制使用。
@dataclass(frozen=True)
class TuiTextSelection:
    anchor: Point
    focus: Point


# LLM: Viewport snapshots are process-local presentation state keyed by the exact
# typed store. They must never be persisted or treated as conversation position.
# 类用途: 保存一个主代理或子代理页面离开时的滚动位置、跟随状态和未读基线。
@dataclass(frozen=True)
class _TuiViewportState:
    follow: bool
    cursor_line: int
    line_count: int
    unseen_baseline: frozenset[str] | None
    unseen_block_ids: frozenset[str]


_TAIL_VIEWPORT_STATE = _TuiViewportState(True, 0, 1, None, frozenset())


# LLM: 右键复制 latch 只描述 mouse gesture，不读取选区或剪贴板；调用方仍分别持有正文/输入 authority。
# 函数用途: 把右键按下、松开、遗失松开和其它新点击归一成复制、吞事件及下一状态。
def _right_copy_mouse_transition(
    mouse_event: MouseEvent,
    armed: bool,
) -> tuple[bool, bool, bool]:
    if (
        mouse_event.event_type == MouseEventType.MOUSE_DOWN
        and mouse_event.button == MouseButton.RIGHT
    ):
        return True, True, True
    if (
        mouse_event.event_type == MouseEventType.MOUSE_UP
        and mouse_event.button == MouseButton.RIGHT
    ):
        # 部分 SSH/tmux/终端组合只转发右键 release；没有对应 down 时也要
        # 完成一次复制，完整 down+up 序列仍由 armed 去重。
        return (not armed), True, False
    if not armed:
        return False, False, False
    if mouse_event.event_type == MouseEventType.MOUSE_UP or (
        mouse_event.event_type == MouseEventType.MOUSE_MOVE
        and mouse_event.button == MouseButton.NONE
    ):
        return False, True, False
    if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
        return False, False, False
    return False, True, True


# LLM: TuiFrameProvider 是 snapshot/context 到 frame 的单帧 memoization 层；block LRU 仍负责跨帧稳定历史复用。
# 类用途: 让 transcript、overlay 和 footer 在一次 prompt_toolkit render 中共享同一画面。
class TuiFrameProvider:
    # LLM: context_factory 必须由调用方显式提供 width→context，provider 不读取全局配置或 wall clock。
    # 函数用途: 创建 frame provider 并订阅 typed state store 更新。
    def __init__(
        self,
        state_store: TuiStateStore,
        context_factory: Callable[[int], TuiRenderContext],
        *,
        block_cache: TuiBlockRenderCache | None = None,
        transcript_state: TuiTranscriptModeState | None = None,
    ) -> None:
        self.state_store = state_store
        self.context_factory = context_factory
        self.block_cache = block_cache or TuiBlockRenderCache()
        self.transcript_state = transcript_state
        self.last_width = DEFAULT_TRANSCRIPT_WIDTH
        self._cached_key: tuple[Any, ...] | None = None
        self._cached_frame: TuiRenderFrame | None = None
        self._invalidate_callback: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._subscribed_store_ids: set[int] = {id(state_store)}
        self.state_store.subscribe(self.invalidate)
        if self.transcript_state is not None:
            self.transcript_state.set_invalidate_callback(self.invalidate)

    # LLM: View navigation may swap only the visible typed store. Every seen
    # store remains subscribed to this provider, while canonical state and event
    # sequencing stay inside their original runtimes.
    # 函数用途: 进入或返回代理详情时切换当前画面使用的状态仓库。
    def set_state_store(self, state_store: TuiStateStore) -> None:
        if not isinstance(state_store, TuiStateStore):
            raise TypeError("state_store must be TuiStateStore")
        with self._lock:
            if self.state_store is state_store:
                return
            self.state_store = state_store
            self._cached_key = None
            needs_subscription = id(state_store) not in self._subscribed_store_ids
            self._subscribed_store_ids.add(id(state_store))
        if needs_subscription:
            state_store.subscribe(self.invalidate)
        self.invalidate()

    # LLM: frame key覆盖 block版本、queue、permission、status 与 context；diagnostics 不可见所以不触发重绘。
    # 函数用途: 返回当前宽度的共享渲染帧。
    def frame(self, width: int) -> TuiRenderFrame:
        normalized_width = max(1, int(width or 1))
        live_snapshot = self.state_store.snapshot()
        snapshot = (
            self.transcript_state.snapshot_for_render(live_snapshot)
            if self.transcript_state is not None
            else live_snapshot
        )
        context = self.context_factory(normalized_width)
        transcript_key = (
            _transcript_frame_key(self.transcript_state)
            if self.transcript_state is not None
            else None
        )
        key = _frame_key(snapshot, context, transcript_key)
        with self._lock:
            self.last_width = normalized_width
            if key == self._cached_key and self._cached_frame is not None:
                return self._cached_frame
        rendered = render_tui_snapshot(snapshot, context, cache=self.block_cache)
        if self.transcript_state is not None:
            rendered = self.transcript_state.decorate_frame(
                rendered,
                width=normalized_width,
            )
        rendered = sanitize_tui_render_frame(rendered)
        with self._lock:
            self._cached_key = key
            self._cached_frame = rendered
        return rendered

    # LLM: invalidate 只丢单帧 memo 并请求 app redraw；不能在 worker callback 中直接创建 UIContent。
    # 函数用途: 响应 typed event 发布或动画 tick。
    def invalidate(self) -> None:
        with self._lock:
            self._cached_key = None
            callback = self._invalidate_callback
        if callback is not None:
            callback()

    # LLM: app invalidator 在 Application 创建后绑定，替换时不累积多个回调。
    # 函数用途: 设置线程安全的 redraw 请求函数。
    def set_invalidate_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._invalidate_callback = callback


# LLM: Store switching mutates only one control's process-local viewport fields
# under its lock. The provider swap remains owned by TuiTranscriptView so both
# normal/modal controls move together before the next render.
# 函数用途: 保存当前代理页面滚动状态、恢复目标页面状态，并清掉不能跨页面使用的文本选区。
def _switch_transcript_control_store(
    control: TuiTranscriptControl,
    state_store: TuiStateStore,
) -> None:
    if not isinstance(state_store, TuiStateStore):
        raise TypeError("state_store must be TuiStateStore")
    with control._lock:
        if control._state_store is state_store:
            return
        control._viewport_by_store[control._state_store] = _TuiViewportState(
            follow=control.follow,
            cursor_line=control.cursor_line,
            line_count=control._line_count,
            unseen_baseline=control._unseen_baseline,
            unseen_block_ids=control._unseen_block_ids,
        )
        restored = control._viewport_by_store.get(state_store, _TAIL_VIEWPORT_STATE)
        control._state_store = state_store
        (
            control.follow,
            control.cursor_line,
            control._line_count,
            control._unseen_baseline,
            control._unseen_block_ids,
        ) = (
            restored.follow,
            max(0, restored.cursor_line),
            max(1, restored.line_count),
            restored.unseen_baseline,
            restored.unseen_block_ids,
        )
        (
            control._selection,
            control._selection_dragging,
            control._selection_copied,
            control._right_copy_armed,
            control._selection_width,
            control._last_lines,
        ) = (None, False, False, False, 0, ())


# LLM: TuiTranscriptControl 只投影 provider transcript 行并维护可视 cursor anchor；它不复制 transcript 字符串。
# 类用途: 给 prompt_toolkit Window 提供按需 formatted lines、follow-tail 和手动滚动。
class TuiTranscriptControl(UIControl):
    # LLM: cursor_line 是渲染锚点而非会话位置；新消息仅在 follow=True 时把锚点推进末尾。
    # 函数用途: 创建一个非 focusable 的滚动 transcript control。
    def __init__(self, provider: TuiFrameProvider) -> None:
        self.provider = provider
        self._state_store = provider.state_store
        self._viewport_by_store: dict[TuiStateStore, _TuiViewportState] = {}
        self.follow = True
        self.cursor_line = 0
        self._line_count = 1
        self._unseen_baseline: frozenset[str] | None = None
        self._unseen_block_ids: frozenset[str] = frozenset()
        self._selection: TuiTextSelection | None = None
        self._selection_dragging = False
        self._selection_copied = False
        self._right_copy_armed = False
        self._copy_on_select: Callable[[str], None] | None = None
        self._selection_width = 0
        self._last_lines: tuple[FormattedLine, ...] = ()
        self._lock = threading.Lock()

    # LLM: A source switch saves only viewport state under the exact typed store,
    # restores an existing page or initializes a new page at sticky tail, and
    # always clears selection so text coordinates cannot cross agent boundaries.
    # 函数用途: 切换主代理/子代理正文来源时分别保存和恢复各自的滚动位置。
    def switch_state_store(self, state_store: TuiStateStore) -> None:
        _switch_transcript_control_store(self, state_store)

    # LLM: modal transcript 必须可获得焦点以隔离普通输入 Buffer；control 本身仍只处理全局 keybindings，不接收正文编辑。
    # 函数用途: 允许 Layout 在 Ctrl-O 模式把键盘焦点放到 transcript viewport。
    def is_focusable(self) -> bool:
        return True

    # LLM: create_content 每次只取共享 frame 引用，不拼整份字符串；空 transcript 仍返回一行合法 UIContent。
    # 函数用途: 生成 prompt_toolkit 当前宽度的行访问器和滚动光标。
    def create_content(self, width: int, height: int) -> UIContent:
        del height
        lines = self.provider.frame(width).transcript_lines or ((),)
        visible_block_ids = _counted_message_block_ids(
            self.provider.state_store.snapshot()
        )
        with self._lock:
            if self._selection_width and self._selection_width != width:
                self._selection = None
                self._selection_dragging = False
                self._right_copy_armed = False
            self._selection_width = width
            self._last_lines = tuple(lines)
            self._line_count = len(lines)
            if self.follow:
                self.cursor_line = len(lines) - 1
                self._unseen_baseline = None
                self._unseen_block_ids = frozenset()
            else:
                self.cursor_line = min(self.cursor_line, len(lines) - 1)
                self._refresh_unseen_locked(visible_block_ids)
            cursor_line = max(0, self.cursor_line)
            selection = self._selection
        return UIContent(
            get_line=lambda index: _prompt_toolkit_line(
                _decorate_selection(lines[index], index, selection)
            ),
            line_count=len(lines),
            cursor_position=Point(x=0, y=cursor_line),
            show_cursor=False,
        )

    # LLM: inline Application 需要内容的真实 preferred height；上限由 prompt_toolkit 可用高度裁剪，不复制行正文。
    # 函数用途: 让短 transcript 紧贴输入框，长 transcript 使用当前终端可用区域。
    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix: Any,
    ) -> int | None:
        del wrap_lines, get_line_prefix
        line_count = len(self.provider.frame(width).transcript_lines) or 1
        return min(line_count, max(1, int(max_available_height or 1)))

    # LLM: Upward/manual movement breaks sticky-tail, while a downward move that reaches the
    # current last rendered line restores it. This mirrors 终端交互's isSticky contract and
    # must clear unseen state without relying on footer text.
    # 函数用途: 上下移动 transcript；向下回到底部时自动恢复跟随新消息。
    def move(self, delta: int) -> None:
        visible_block_ids = _counted_message_block_ids(
            self.provider.state_store.snapshot()
        )
        with self._lock:
            self._begin_manual_scroll_locked(visible_block_ids)
            target = max(
                0,
                min(self._line_count - 1, self.cursor_line + int(delta)),
            )
            self.cursor_line = target
            if int(delta) > 0 and target >= self._line_count - 1:
                self.follow = True
                self._unseen_baseline = None
                self._unseen_block_ids = frozenset()
            else:
                self.follow = False

    # LLM: home 显式离开 follow-tail 并把 anchor 设为首行。
    # 函数用途: 跳到 transcript 顶部。
    def move_home(self) -> None:
        visible_block_ids = _counted_message_block_ids(
            self.provider.state_store.snapshot()
        )
        with self._lock:
            self._begin_manual_scroll_locked(visible_block_ids)
            self.follow = False
            self.cursor_line = 0

    # LLM: end 恢复 follow-tail；具体末行在下一 create_content 根据当前 frame 决定。
    # 函数用途: 跳到 transcript 底部并继续跟随新输出。
    def move_end(self) -> None:
        with self._lock:
            self.follow = True
            self.cursor_line = max(0, self._line_count - 1)
            self._unseen_baseline = None
            self._unseen_block_ids = frozenset()

    # LLM: jump_to 使用已渲染行坐标并关闭 follow；搜索命中不会通过修改 Window 私有 scroll 字段实现。
    # 函数用途: 将 transcript 锚点跳到指定可见行。
    def jump_to(self, line_index: int) -> None:
        visible_block_ids = _counted_message_block_ids(
            self.provider.state_store.snapshot()
        )
        with self._lock:
            self._begin_manual_scroll_locked(visible_block_ids)
            self.follow = False
            self.cursor_line = max(0, min(self._line_count - 1, int(line_index or 0)))

    # LLM: current_line 返回 control 自己的结构化锚点，搜索入口无需读取 prompt_toolkit render_info 私有字段。
    # 函数用途: 取得当前 transcript 滚动行。
    def current_line(self) -> int:
        with self._lock:
            return max(0, int(self.cursor_line))

    # LLM: 指示器只根据 follow flag 和滚动时冻结的 typed block ids 计算，不能以 transcript 文案或行数猜消息数。
    # 函数用途: 返回 `Jump to bottom` 或新增消息数；位于尾部时返回 None。
    def scroll_indicator(self) -> str | None:
        visible_block_ids = _counted_message_block_ids(
            self.provider.state_store.snapshot()
        )
        with self._lock:
            if self.follow:
                return None
            self._refresh_unseen_locked(visible_block_ids)
            count = len(self._unseen_block_ids)
        if count <= 0:
            return "Jump to bottom"
        noun = "message" if count == 1 else "messages"
        return f"{count} new {noun}"

    # LLM: 复制只读取最近一次 create_content 的可见行与结构化选区；没有非空选区时返回空串，调用方才能继续执行中断。
    # 函数用途: 返回当前鼠标选中的纯文本，跨行时用换行连接。
    def selected_text(self) -> str:
        with self._lock:
            selection = self._selection
            lines = self._last_lines
        return _selected_text(lines, selection) if selection is not None else ""

    # LLM: 全选（Ctrl+A）——选中全部可见行，配合 Ctrl+C 复制（transcript 模式
    # 键盘复制入口）。选择坐标基于最近一次 create_content 的行数。
    # 函数用途: 把整个 transcript 视图设为选区，供键盘复制。
    def select_all(self) -> None:
        with self._lock:
            lines = self._last_lines
            if not lines:
                return
            last_line = len(lines) - 1
            last_char = max(0, len(fragments_text(lines[last_line])) - 1)
            self._selection = TuiTextSelection(
                anchor=Point(0, 0),
                focus=Point(last_char, last_line),
            )
            self._selection_dragging = False
            self._selection_copied = False
        self.provider.invalidate()

    # LLM: 清理只影响当前 viewport 选区，不清 transcript、滚动锚点或输入草稿。
    # 函数用途: 取消当前文本选区。
    def clear_selection(self) -> None:
        with self._lock:
            self._selection = None
            self._selection_dragging = False
            self._selection_copied = False
            self._right_copy_armed = False

    # LLM: Copy-on-select is a UI projection callback installed only after Application exists;
    # selection coordinates and clipboard transport remain separate authorities.
    # 函数用途: 绑定鼠标松手后的复制动作；传入 None 时只保留高亮而不写剪贴板。
    def set_copy_on_select(self, callback: Callable[[str], None] | None) -> None:
        with self._lock:
            self._copy_on_select = callback

    # LLM: Every terminal release shape, lost-release recovery, and fresh-press fallback must end
    # one drag exactly once. The callback runs outside the selection lock to avoid UI re-entry.
    # 函数用途: 收口当前拖选、按可选终点更新范围，并把最终可见文本自动复制一次。
    def _finish_selection(self, point: Point | None = None) -> bool:
        callback: Callable[[str], None] | None = None
        selected_text = ""
        with self._lock:
            selection = self._selection
            if selection is None or not self._selection_dragging:
                return False
            if point is not None:
                selection = TuiTextSelection(
                    selection.anchor,
                    _bounded_selection_point(point, self._line_count),
                )
                self._selection = selection
            self._selection_dragging = False
            if not self._selection_copied:
                selected_text = _selected_text(self._last_lines, selection)
                self._selection_copied = True
                callback = self._copy_on_select
        self.provider.invalidate()
        if callback is not None and selected_text.strip():
            callback(selected_text)
        return True

    # LLM: baseline 只在从 follow 进入手动滚动时冻结，继续滚动不能吞掉已累计的未读 block。
    # 函数用途: 开始一次离尾浏览并记录当时已存在的可计数消息。
    def _begin_manual_scroll_locked(self, visible_block_ids: frozenset[str]) -> None:
        if self.follow or self._unseen_baseline is None:
            self._unseen_baseline = visible_block_ids
            self._unseen_block_ids = frozenset()

    # LLM: 新消息集合由当前 typed user/assistant block ids 减 baseline 得到；active→stable 的同 id 不能重复计数。
    # 函数用途: 刷新离尾期间累计的未读消息身份。
    def _refresh_unseen_locked(self, visible_block_ids: frozenset[str]) -> None:
        baseline = self._unseen_baseline
        if baseline is None:
            self._unseen_baseline = visible_block_ids
            self._unseen_block_ids = frozenset()
            return
        self._unseen_block_ids = visible_block_ids - baseline

    # LLM: 选区只在明确 left-down 到对应 up 的拖动窗口内更新；右键只重复投影已有选区到剪贴板，不能改坐标或清高亮。
    # 函数用途: 支持滚轮浏览、有起止边界的鼠标拖选，以及选中后右键直接复制。
    def mouse_handler(self, mouse_event: MouseEvent):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.move(-3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.move(3)
            return None
        if _handle_transcript_right_copy(self, mouse_event):
            return None
        if (
            mouse_event.event_type == MouseEventType.MOUSE_DOWN
            and mouse_event.button == MouseButton.LEFT
        ):
            # 终端交互 同类兜底：模式 1002 终端可能在窗口外丢失上一轮 release；
            # 新 press 先收口旧拖选，不能让旧 anchor 延续到下一次点击。
            self._finish_selection()
            point = _bounded_selection_point(mouse_event.position, self._line_count)
            with self._lock:
                self._selection = TuiTextSelection(point, point)
                self._selection_dragging = True
                self._selection_copied = False
            self.provider.invalidate()
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if mouse_event.button != MouseButton.LEFT:
                # 终端启用 1003 后，无按键 motion 是窗口外松手的可靠补偿信号。
                return None if self._finish_selection() else NotImplemented
            with self._lock:
                selection = self._selection
                if selection is None or not self._selection_dragging:
                    return NotImplemented
                self._selection = TuiTextSelection(
                    selection.anchor,
                    _bounded_selection_point(mouse_event.position, self._line_count),
                )
            self.provider.invalidate()
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            # 不筛 button：部分终端把 release 编成 NONE/UNKNOWN 或保留 motion bit。
            return None if self._finish_selection(mouse_event.position) else NotImplemented
        return NotImplemented


# LLM: transcript 右键复制只读取当前 control 的可见选区并更新 gesture latch；clipboard callback 必须在锁外执行。
# 函数用途: 处理正文右键复制及遗失 release，返回本次鼠标事件是否已被消费。
def _handle_transcript_right_copy(
    control: TuiTranscriptControl,
    mouse_event: MouseEvent,
) -> bool:
    with control._lock:
        armed = control._right_copy_armed
    copy_requested, consume_event, next_armed = _right_copy_mouse_transition(
        mouse_event,
        armed,
    )
    callback: Callable[[str], None] | None = None
    selected_text = ""
    with control._lock:
        control._right_copy_armed = next_armed
        if copy_requested:
            selected_text = _selected_text(control._last_lines, control._selection)
            callback = control._copy_on_select
    if callback is not None and selected_text.strip():
        callback(selected_text)
    return consume_event


# LLM: TuiTranscriptView 是 setup 层的控件束，keybindings 通过 control 方法滚动而不是修改 TextArea buffer。
# 类用途: 汇总共享 provider、transcript Window、固定 Todo/代理/状态区、overlay/footer controls 和滚动入口。
@dataclass(frozen=True)
class TuiTranscriptView:
    provider: TuiFrameProvider
    control: TuiTranscriptControl
    window: Window
    modal_control: TuiTranscriptControl
    modal_window: Window
    transcript_state: TuiTranscriptModeState
    overlay_control: FormattedTextControl
    input_status_control: FormattedTextControl
    todo_control: FormattedTextControl
    agent_control: FormattedTextControl
    footer_control: FormattedTextControl

    # LLM: Agent navigation must switch the provider and both normal/modal
    # controls as one UI operation. Existing pages restore their own viewport;
    # a first visit starts at tail without clearing either typed transcript.
    # 函数用途: 在主代理和子代理页面之间切换正文，并保留每个页面自己的上翻位置。
    def set_state_store(self, state_store: TuiStateStore) -> None:
        if not isinstance(state_store, TuiStateStore):
            raise TypeError("state_store must be TuiStateStore")
        if self.provider.state_store is state_store:
            return
        self.control.switch_state_store(state_store)
        self.modal_control.switch_state_store(state_store)
        self.provider.set_state_store(state_store)

    # LLM: scroll 委托 control 并由 setup/app invalidate，view 不持有 Application 反向引用。
    # 函数用途: 按行移动 transcript anchor。
    def scroll(self, delta: int) -> None:
        self._active_control().move(delta)

    # LLM: home 委托 control 的结构化锚点，不访问私有 Window scroll 字段。
    # 函数用途: 跳到 transcript 首行。
    def home(self) -> None:
        self._active_control().move_home()

    # LLM: end 恢复 follow-tail，不根据当前显示文本长度计算位置。
    # 函数用途: 跳到 transcript 末尾。
    def end(self) -> None:
        self._active_control().move_end()

    # LLM: jump_search_match 只接收 transcript_state 计算出的可见行，不能按搜索文案重新扫描正文。
    # 函数用途: 将详细 transcript 滚动到当前搜索命中。
    def jump_search_match(self, line_index: int | None) -> None:
        if line_index is None:
            return
        self.modal_control.jump_to(line_index)

    # LLM: current_line 根据结构化 mode 选择普通或 modal control；两个 viewport 不共享隐式 Window 状态。
    # 函数用途: 取得当前可见 transcript 的滚动锚点。
    def current_line(self) -> int:
        return self._active_control().current_line()

    # LLM: Ctrl-C 只能读取当前普通/modal viewport 的选区；空选区必须让上层继续走中断或退出语义。
    # 函数用途: 返回当前可见 transcript 的鼠标选中文本。
    def selected_text(self) -> str:
        return self._active_control().selected_text()

    # LLM: 清选区委托当前 viewport，不影响另一模式被冻结的消息状态。
    # 函数用途: 清除当前可见 transcript 的选区。
    def clear_selection(self) -> None:
        self._active_control().clear_selection()

    # LLM: Both normal and detailed transcript controls share one clipboard projection callback;
    # whichever viewport is active owns the selection and invokes it only when its drag settles.
    # 函数用途: 为普通与详细 transcript 同时启用松手自动复制。
    def set_copy_on_select(self, callback: Callable[[str], None] | None) -> None:
        self.control.set_copy_on_select(callback)
        self.modal_control.set_copy_on_select(callback)

    # LLM: footer 高度来自当前 frame 的显式换行与滚动指示器，不能固定为一行而裁掉 help 菜单。
    # 函数用途: 告诉 prompt_toolkit 当前 footer 应占多少终端行。
    def footer_line_count(self) -> int:
        fragments = _decorated_footer(
            self.provider,
            self._active_control(),
        )
        text = "".join(str(fragment[1]) for fragment in fragments)
        return max(1, text.count("\n") + 1)

    # LLM: active control 的选择只读 transcript mode 布尔值，不能根据 footer 或焦点推断。
    # 函数用途: 返回当前应接收滚动命令的 transcript 控件。
    def _active_control(self) -> TuiTranscriptControl:
        return self.modal_control if self.transcript_state.snapshot().active else self.control


# LLM: view factory 创建共享 provider 后把所有固定控件绑定同一 frame；不创建第二状态 store。
# 函数用途: 构造新的 typed transcript 视图。
def make_tui_transcript_view(
    state_store: TuiStateStore,
    context_factory: Callable[[int], TuiRenderContext],
    *,
    transcript_state: TuiTranscriptModeState | None = None,
) -> TuiTranscriptView:
    mode_state = transcript_state or TuiTranscriptModeState()
    provider = TuiFrameProvider(
        state_store,
        context_factory,
        transcript_state=mode_state,
    )
    control = TuiTranscriptControl(provider)
    window = Window(
        content=control,
        wrap_lines=False,
        dont_extend_height=True,
        always_hide_cursor=True,
        allow_scroll_beyond_bottom=False,
        style="class:tui-transcript",
    )
    modal_control = TuiTranscriptControl(provider)
    modal_window = Window(
        content=modal_control,
        wrap_lines=False,
        dont_extend_height=False,
        always_hide_cursor=True,
        allow_scroll_beyond_bottom=False,
        style="class:tui-transcript",
    )
    overlay_control = FormattedTextControl(lambda: _flatten_lines(provider.frame(provider.last_width).overlay_lines))
    input_status_control = FormattedTextControl(
        lambda: _flatten_lines(provider.frame(provider.last_width).input_status_lines)
    )
    todo_control = FormattedTextControl(
        lambda: _flatten_lines(provider.frame(provider.last_width).todo_lines)
    )
    agent_control = FormattedTextControl(
        lambda: _flatten_lines(provider.frame(provider.last_width).agent_lines)
    )
    def active_control() -> TuiTranscriptControl:
        return modal_control if mode_state.snapshot().active else control

    footer_control = FormattedTextControl(
        lambda: _decorated_footer(provider, active_control())
    )
    return TuiTranscriptView(
        provider,
        control,
        window,
        modal_control,
        modal_window,
        mode_state,
        overlay_control,
        input_status_control,
        todo_control,
        agent_control,
        footer_control,
    )


# LLM: 未读指示器只装饰 renderer footer，不写回 TuiViewModel；其计数来自 control 冻结的 typed block identities。
# 函数用途: 在离开尾部后追加 终端交互 风格的跳底/新消息 pill 文案。
def _decorated_footer(
    provider: TuiFrameProvider,
    control: TuiTranscriptControl,
) -> list[tuple[Any, ...]]:
    footer: list[tuple[Any, ...]] = list(provider.frame(provider.last_width).footer)
    indicator = control.scroll_indicator()
    if indicator is None:
        return footer
    if footer:
        footer.append(("", "  "))
    footer.append(
        (
            "class:tui-new-messages",
            f" {indicator} ↓ ",
            _jump_to_bottom_mouse_handler(provider, control),
        )
    )
    return footer


# LLM: footer pill 只在左键释放时恢复当前 viewport 的 follow-tail；它不根据显示文案定位消息，也不修改 transcript/state store。
# 函数用途: 让 `N new messages ↓` 像 终端交互 一样可点击并立即回到最新消息。
def _jump_to_bottom_mouse_handler(
    provider: TuiFrameProvider,
    control: TuiTranscriptControl,
) -> Callable[[MouseEvent], Any]:
    def handle(mouse_event: MouseEvent):
        if (
            mouse_event.event_type == MouseEventType.MOUSE_UP
            and mouse_event.button == MouseButton.LEFT
        ):
            control.move_end()
            provider.invalidate()
            return None
        return NotImplemented

    return handle


# LLM: 终端交互 的 unseen count 只统计用户可理解的新对话消息；tool/thinking/session block 不单独增加数字。
# 函数用途: 从 typed snapshot 提取当前可计数的 user/assistant block identities。
def _counted_message_block_ids(snapshot: TuiViewSnapshot) -> frozenset[str]:
    return frozenset(
        block.block_id
        for block in (*snapshot.stable_blocks, *snapshot.active_blocks)
        if block.role in {"user", "assistant"} and bool(block.text or block.detail)
    )


# LLM: frame key 使用显示版本字段而非 dataclass payload repr，避免 secret/未知 metadata 进入缓存身份。
# 函数用途: 生成共享 frame 的可哈希版本键。
def _frame_key(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
    transcript_key: tuple[Any, ...] | None = None,
) -> tuple[Any, ...]:
    blocks = tuple(
        (block.block_id, block.updated_seq, block.phase)
        for block in (*snapshot.stable_blocks, *snapshot.active_blocks)
    )
    queue = tuple((item.queue_id, item.seq, item.priority, item.text) for item in snapshot.queued_inputs)
    permission = _permission_key(snapshot)
    return (
        blocks,
        queue,
        permission,
        snapshot.status,
        tui_render_context_key(snapshot, context),
        transcript_key,
    )


# LLM: transcript frame key 排除计算结果 count/current，只含会改变输入 frame 的模式与 query，避免 decorate 后自触发二次失效。
# 函数用途: 生成 transcript UI 状态的有界缓存键。
def _transcript_frame_key(state: TuiTranscriptModeState) -> tuple[Any, ...]:
    snapshot = state.snapshot()
    return (
        snapshot.active,
        snapshot.show_all,
        snapshot.search_open,
        snapshot.search_query,
    )


# LLM: permission cache key 只含 overlay 公开选项/id/selection，不复制其它潜在 payload。
# 函数用途: 生成当前权限面板的可哈希版本键。
def _permission_key(snapshot: TuiViewSnapshot) -> tuple[Any, ...] | None:
    permission = snapshot.permission
    if permission is None:
        return None
    options = tuple(
        (str(option.get("id") or ""), str(option.get("label") or ""))
        for option in permission.options
    )
    return (
        permission.permission_id,
        permission.block_id,
        permission.title,
        permission.description,
        permission.selected_index,
        permission.feedback_mode,
        permission.feedback,
        permission.feedback_placeholder,
        options,
    )


# LLM: prompt_toolkit line 转换只复制单行 tuple 外壳，不拼正文或改变 style 字符串。
# 函数用途: 将内部不可变行适配为 StyleAndTextTuples。
def _prompt_toolkit_line(line: FormattedLine) -> StyleAndTextTuples:
    return list(line)


# LLM: Window 已把屏幕列反解为 UIContent 源字符索引；这里只限制行和负索引，不能再按 wcwidth 二次换算。
# 函数用途: 把鼠标位置规范为合法 transcript 选区端点。
def _bounded_selection_point(point: Point, line_count: int) -> Point:
    return Point(
        x=max(0, int(point.x or 0)),
        y=max(0, min(max(0, int(line_count or 1) - 1), int(point.y or 0))),
    )


# LLM: selection range 保存 prompt_toolkit 已换算好的源字符索引；focus 字符仍按包含式选中。
# 函数用途: 返回从上到下、从左到右排列的选区端点。
def _ordered_selection(selection: TuiTextSelection) -> tuple[Point, Point]:
    anchor_key = (selection.anchor.y, selection.anchor.x)
    focus_key = (selection.focus.y, selection.focus.x)
    return (
        (selection.anchor, selection.focus)
        if anchor_key <= focus_key
        else (selection.focus, selection.anchor)
    )


# LLM: 复制从 formatted line 的可见文本投影生成，并直接按源字符索引切片；中文宽字符不可再次换算显示列。
# 函数用途: 提取选区中的纯文本并保留跨行换行。
def _selected_text(
    lines: tuple[FormattedLine, ...],
    selection: TuiTextSelection,
) -> str:
    if not lines:
        return ""
    start, end = _ordered_selection(selection)
    if start == end:
        return ""
    selected: list[str] = []
    last_line = min(end.y, len(lines) - 1)
    for line_index in range(max(0, start.y), last_line + 1):
        text = fragments_text(lines[line_index])
        start_index = start.x if line_index == start.y else 0
        end_index = end.x + 1 if line_index == end.y else len(text)
        selected.append(text[max(0, start_index) : max(0, end_index)])
    return "\n".join(selected)


# LLM: 高亮只叠加 style role，不把选区正文复制进 renderer cache；索引与 Window 传给 mouse handler 的源字符坐标一致。
# 函数用途: 给选区覆盖到的字符附加选择背景样式。
def _decorate_selection(
    line: FormattedLine,
    line_index: int,
    selection: TuiTextSelection | None,
) -> FormattedLine:
    if selection is None:
        return line
    start, end = _ordered_selection(selection)
    if start == end or line_index < start.y or line_index > end.y:
        return line
    lower = start.x if line_index == start.y else 0
    upper = end.x + 1 if line_index == end.y else len(fragments_text(line))
    if upper <= lower:
        return line
    decorated: list[tuple[str, str]] = []
    source_index = 0
    for style, text, *_handler in line:
        for char in text:
            selected = lower <= source_index < upper
            selected_style = f"{style} class:tui-selection".strip() if selected else style
            if decorated and decorated[-1][0] == selected_style:
                decorated[-1] = (selected_style, decorated[-1][1] + char)
            else:
                decorated.append((selected_style, char))
            source_index += 1
    return tuple(decorated)


# LLM: 多行 control 的 newline 在此统一插入，末行不额外添加换行避免 overlay 高度漂移。
# 函数用途: 展平 overlay 行供 FormattedTextControl 使用。
def _flatten_lines(lines: tuple[FormattedLine, ...]) -> StyleAndTextTuples:
    fragments: StyleAndTextTuples = []
    for index, line in enumerate(lines):
        fragments.extend(line)
        if index < len(lines) - 1:
            fragments.append(("", "\n"))
    return fragments


__all__ = [
    "TuiFrameProvider",
    "TuiTranscriptControl",
    "TuiTranscriptView",
    "make_tui_transcript_view",
]
