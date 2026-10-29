# LLM: 本模块把 typed snapshot renderer 接入 prompt_toolkit；缓存、全选与复制随当前 store/viewport 路由，只管显示；空选区右键不得触发业务动作。
# 模块用途: 提供正文、提示和独立用户顶栏；顶栏不进入正文/复制，布局与点击只使用当前帧和阅读锚点。

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from .tui_block_renderer import (
    TuiBlockRenderCache,
    TuiRenderContext,
    TuiRenderFrame,
    render_tui_snapshot,
    sanitize_tui_render_frame,
    tui_render_context_key,
)
from .tui_markdown import (
    FormattedLine,
    _graphemes,
    display_width_text,
    fragments_text,
    sanitize_terminal_text,
)
from .tui_reading import (
    ReadingAnchor,
    capture_reading_anchor,
    capture_viewport_top_anchor,
    detail_reflow_top,
    reading_anchor_top,
    scroll_detail_window,
    scrollable_line_count,
    viewport_top,
)
from .tui_transcript import TuiTranscriptModeState, complete_detail_window
from .tui_view_model import TuiStateStore, TuiUserTurnLine, TuiViewSnapshot

# 默认转录面板宽度 80 字符
DEFAULT_TRANSCRIPT_WIDTH_CHARS = 80


# LLM: 选区坐标只属于当前宽度下的可见 transcript；resize 会清除，不能持久化或成为会话正文引用。
# 类用途: 保存鼠标拖选的起点和终点，供高亮与 Ctrl-C 复制使用。
@dataclass(frozen=True)
class TuiTextSelection:
    anchor: Point
    focus: Point


# LLM: 视口快照是按精确 typed store 归档的进程内展示态，严禁持久化落盘或当作会话历史位置使用。
# 类用途: 保存一个主代理或子代理页面离开时的滚动位置、跟随状态和未读基线。
@dataclass(frozen=True)
class _TuiViewportState:
    follow: bool
    cursor_line: int
    line_count: int
    unseen_baseline: frozenset[str] | None
    unseen_block_ids: frozenset[str]


_TAIL_VIEWPORT_STATE = _TuiViewportState(True, 0, 1, None, frozenset())
_HEAD_VIEWPORT_STATE = _TuiViewportState(False, 0, 1, None, frozenset())


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


# LLM: 净化、压空白后按终端grapheme列宽截断，始终为省略号留一列，不拆组合emoji或执行终端控制字符。
# 函数用途: 生成安全、紧凑且不超过终端可用宽度的单行用户摘要。
def _single_line_user_summary(text: str, width: int) -> str:
    width = max(1, width)
    normalized = " ".join(sanitize_terminal_text(text).split())
    if display_width_text(normalized) <= width:
        return normalized
    parts, used = [], 0
    for cluster in _graphemes(normalized):
        size = display_width_text(cluster)
        if used + size > width - 1:
            break
        parts.append(cluster)
        used += size
    return "".join(parts) + "…"


# LLM: 普通视图按真实offset；完整原文保留全历史用户目标，窗口只补物理首行，续页不能冒充另一回合或制造全局行号。
# 函数用途: 合成用户消息导航索引，明确区分窗口内的物理行和窗口外的块锚点。
def _user_turn_line_index(
    snapshot: TuiViewSnapshot, frame: TuiRenderFrame, width: int,
    *, complete_turns: tuple[TuiUserTurnLine, ...] | None = None,
) -> tuple[TuiUserTurnLine, ...]:
    starts = frame.block_line_offsets
    if frame.line_origins:
        starts = tuple((key, index) for index, (key, source_row) in enumerate(frame.line_origins) if source_row == 0)
    if complete_turns is not None:
        visible_starts: dict[str, int] = {}
        for key, start in starts:
            visible_starts.setdefault(key, start)
        return tuple(replace(row, start_line=visible_starts.get(row.block_id)) for row in complete_turns)
    users = {block.block_id: block for block in (*snapshot.stable_blocks, *snapshot.active_blocks) if block.role == "user"}
    result, seen = [], set()
    for key, start in starts:
        if key not in users or key in seen:
            continue
        seen.add(key)
        block = users[key]
        summary = _single_line_user_summary(block.text or block.detail or block.title or "（无文字消息）", width)
        result.append(TuiUserTurnLine(key, start, summary))
    return tuple(sorted(result, key=lambda row: row.start_line))


# LLM: 仅缓存当前snapshot/context的帧、用户行索引和块键；索引取最终排版坐标，不能从正文猜角色或写入模型历史。
# 类用途: 让正文和底栏共享画面；活动增量不让每个控件重复遍历全部稳定历史。
class TuiFrameProvider:
    # LLM: context_factory显式提供width→context；完整用户索引强持一组冻结来源与宽度，不缓存整段排版或跨来源复用。
    # 函数用途: 创建共享帧和轻量用户块索引缓存，并订阅typed state store更新。
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
        self.last_width = DEFAULT_TRANSCRIPT_WIDTH_CHARS
        self._cached_key: tuple[Any, ...] | None = None
        self._cached_frame: TuiRenderFrame | None = None
        self._cached_user_turn_lines: tuple[TuiUserTurnLine, ...] = ()
        self._cached_block_positions: dict[str, int] = {}
        self._complete_index_sources: tuple[object, object, int] = (None, None, 0)
        self._complete_index: tuple[dict[str, int], tuple[TuiUserTurnLine, ...]] = ({}, ())
        self._block_key_sources: tuple[object, ...] = ()
        self._block_keys: tuple[tuple[Any, ...], ...] = ()
        self._invalidate_callback: Callable[[], None] | None = None
        self.return_to_input: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._subscribed_store_ids: set[int] = {id(state_store)}
        self.state_store.subscribe(self.invalidate)
        if self.transcript_state is not None:
            self.transcript_state.set_invalidate_callback(self.invalidate)

    # LLM: 视图导航只能替换当前可见的 typed store 并清空两类用户提问索引；已订阅的 store 保持订阅，展开态冻结快照重绑到新 store，权威状态仍留在原运行时。
    # 函数用途: 进入或返回代理详情时一起切换正文来源和详细模式快照，不能留下另一代理的旧画面。
    def set_state_store(self, state_store: TuiStateStore) -> None:
        if not isinstance(state_store, TuiStateStore):
            raise TypeError("state_store must be TuiStateStore")
        with self._lock:
            if self.state_store is state_store:
                return
            self.state_store = state_store
            self._cached_key = None
            self._cached_user_turn_lines = ()
            self._cached_block_positions = {}
            self._complete_index_sources = (None, None, 0)
            self._complete_index = ({}, ())
            needs_subscription = id(state_store) not in self._subscribed_store_ids
            self._subscribed_store_ids.add(id(state_store))
        if self.transcript_state is not None:
            self.transcript_state.rebind_view_snapshot(state_store.snapshot())
        if needs_subscription:
            state_store.subscribe(self.invalidate)
        self.invalidate()

    # LLM: frame key覆盖原版本字段；完整用户索引从冻结块建一次，窗口外只持锚点，至多四页排版与索引同代发表。
    # 函数用途: 返回共享帧和用户索引，保留有界完整原文窗口及实时健康提示。
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
        key = _frame_key(snapshot, context, transcript_key, self._frame_blocks_key(snapshot))
        with self._lock:
            self.last_width = normalized_width
            if key == self._cached_key and self._cached_frame is not None:
                return self._cached_frame
        complete = self.transcript_state is not None and self.transcript_state.snapshot().show_all
        block_positions, complete_turns = self._complete_user_index(snapshot, normalized_width) if complete else ({}, None)
        if complete:
            # 原文仅排版锚点附近的有界窗口，页边界不再成为用户必须手动跨越的边界。
            rendered = render_tui_snapshot(
                replace(snapshot, stable_blocks=(), active_blocks=()), context,
            )
            lines, origins, pages, pending = complete_detail_window(self.transcript_state, normalized_width)
            rendered = replace(rendered, transcript_lines=lines, line_origins=origins, detail_page_offsets=pages,
                               pending_detail_blocks=pending)
        else:
            rendered = render_tui_snapshot(snapshot, context, cache=self.block_cache)
        if self.transcript_state is not None:
            rendered = self.transcript_state.decorate_frame(
                rendered,
                width=normalized_width,
                preserve_footer=context.background_sync_failed,
            )
        rendered = sanitize_tui_render_frame(rendered)
        user_lines = _user_turn_line_index(snapshot, rendered, normalized_width, complete_turns=complete_turns)
        with self._lock:
            self._cached_key = key
            self._cached_frame = rendered
            self._cached_user_turn_lines = user_lines
            self._cached_block_positions = block_positions
        return rendered

    # LLM: 按与原文分页相同的typed块顺序建立元数据索引；同冻结tuple/宽度只建一次，不渲染页、不读取归档或live新增块。
    # 函数用途: 缓存全历史用户块顺序和摘要，让跨四页窗口仍可定位上一条消息。
    def _complete_user_index(
        self, snapshot: TuiViewSnapshot, width: int,
    ) -> tuple[dict[str, int], tuple[TuiUserTurnLine, ...]]:
        with self._lock:
            if (snapshot.stable_blocks is self._complete_index_sources[0]
                    and snapshot.active_blocks is self._complete_index_sources[1]
                    and width == self._complete_index_sources[2]):
                return self._complete_index
            blocks = sorted((*snapshot.stable_blocks, *snapshot.active_blocks),
                            key=lambda block: (block.created_seq, block.updated_seq, block.block_id))
            positions = {block.block_id: index for index, block in enumerate(blocks)}
            users = tuple(TuiUserTurnLine(
                block.block_id, None,
                _single_line_user_summary(block.text or block.detail or block.title or "（无文字消息）", width), index,
            ) for index, block in enumerate(blocks) if block.role == "user")
            self._complete_index_sources = (snapshot.stable_blocks, snapshot.active_blocks, width)
            self._complete_index = (positions, users)
            return self._complete_index

    # LLM: 只查同代帧发布的冻结排序，不从窗口内行号推测全历史位置；找不到来源时失败关闭。
    # 函数用途: 为完整原文视口顶部取得与全历史用户索引同一坐标系的块位置。
    def complete_block_position(self, block_id: str) -> int | None:
        with self._lock:
            return self._cached_block_positions.get(block_id)

    # LLM: 仅返回当前同代帧索引；同帧同宽度原样复用，来源切换并发使帧不再当前时失败关闭为空。
    # 函数用途: 给顶栏取得用户导航索引；完整原文窗口外只提供块位置，不谎报物理首行。
    def user_turn_lines(self, width: int | None = None) -> tuple[TuiUserTurnLine, ...]:
        frame = self.frame(self.last_width if width is None else width)
        with self._lock:
            return self._cached_user_turn_lines if self._cached_frame is frame else ()

    # LLM: 强引用当前 immutable tuple，避免裸 id 重用；只保留 stable/active 各一组，历史重排与同块替换必须产生新容器。
    # 函数用途: 分别复用稳定和活动块版本键，活动增量不会重新扫描完整历史。
    def _frame_blocks_key(self, snapshot: TuiViewSnapshot) -> tuple[tuple[Any, ...], ...]:
        sources = (snapshot.stable_blocks, snapshot.active_blocks)
        with self._lock:
            keys = tuple(
                self._block_keys[index]
                if index < len(self._block_key_sources) and blocks is self._block_key_sources[index]
                else _block_versions(blocks)
                for index, blocks in enumerate(sources)
            )
            self._block_key_sources, self._block_keys = sources, keys
        return keys

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


# LLM: store 切换只在控件自身锁内修改单个 control 的进程内视口字段；provider 切换仍由 TuiTranscriptView 统一编排，确保普通与详情控件在下一帧前同步迁移。
# 函数用途: 保存当前代理页面滚动状态、恢复目标页面状态，并清掉不能跨页面使用的文本选区。
def _switch_transcript_control_store(
    control: TuiTranscriptControl,
    state_store: TuiStateStore,
    *,
    start_at_top_if_new: bool = False,
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
        restored = control._viewport_by_store.get(
            state_store,
            _HEAD_VIEWPORT_STATE if start_at_top_if_new else _TAIL_VIEWPORT_STATE,
        )
        control._state_store = state_store
        control._pending_reading_anchor = None
        control._last_reading_frame = None
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
# 类用途: 给 Window 提供有界正文、阅读位置、follow-tail 和直接移动视口的滚动。
class TuiTranscriptControl(UIControl):
    # LLM: 光标只为渲染锚点；计数缓存强持当前stable/active两个tuple，仅源身份变化时重算，不保存业务状态。
    # 函数用途: 初始化滚动、选区与只读消息ID缓存，不发起模型或文件读取。
    def __init__(self, provider: TuiFrameProvider) -> None:
        self.provider = provider
        self._state_store = provider.state_store
        self._viewport_by_store: dict[TuiStateStore, _TuiViewportState] = {}
        self.follow = True
        self.cursor_line = 0
        self._line_count = 1
        self._last_render_height = 1
        self._unseen_baseline: frozenset[str] | None = None
        self._unseen_block_ids: frozenset[str] = frozenset()
        self._counted_sources: tuple[object, object] = (None, None)
        self._counted_ids: frozenset[str] = frozenset()
        self._selection: TuiTextSelection | None = None
        self._selection_dragging = False
        self._selection_copied = False
        self._right_copy_armed = False
        self._copy_on_select: Callable[[str], None] | None = None
        self._older_history: Callable[[bool], None] | None = None
        self._selection_width = 0
        self._last_lines: tuple[FormattedLine, ...] = ()
        self._pending_reading_anchor: ReadingAnchor | None = None
        self._last_reading_frame: TuiRenderFrame | None = None
        self._lock = threading.Lock()

    # LLM: 数据源切换仅按精确 typed store 保存视口状态、恢复已访问页面，并允许首次访问的子代理详情页锚定在提示词边界；选区坐标严禁跨代理共享。
    # 函数用途: 切换主代理/子代理正文来源；首次详情页可从提示词顶部打开，重访仍恢复原位置。
    def switch_state_store(
        self,
        state_store: TuiStateStore,
        *,
        start_at_top_if_new: bool = False,
    ) -> None:
        _switch_transcript_control_store(
            self,
            state_store,
            start_at_top_if_new=start_at_top_if_new,
        )

    # LLM: modal transcript 必须可获得焦点以隔离普通输入 Buffer；control 本身仍只处理全局 keybindings，不接收正文编辑。
    # 函数用途: 允许 Layout 在 Ctrl-O 模式把键盘焦点放到 transcript viewport。
    def is_focusable(self) -> bool:
        return True

    # LLM: 调用方未持control锁；同stable/active容器身份才复用，强引用防id重用，块变更仍由原计数器计算。
    # 函数用途: 返回当前可计数消息ID，渲染与所有滚动入口共享一次历史扫描。
    def _visible_message_block_ids(self, snapshot: TuiViewSnapshot) -> frozenset[str]:
        with self._lock:
            if snapshot.stable_blocks is self._counted_sources[0] and snapshot.active_blocks is self._counted_sources[1]:
                return self._counted_ids
            self._counted_ids = _counted_message_block_ids(snapshot)
            self._counted_sources = (snapshot.stable_blocks, snapshot.active_blocks)
            return self._counted_ids

    # LLM: 原frame/补行/锚点规则不变；保存tuple子类身份以省整段复制，消息ID与滚动共用同一源身份memo。
    # 函数用途: 生成合法行访问器，复用安全行组且保留阅读位置与选区。
    def create_content(self, width: int, height: int) -> UIContent:
        frame = self.provider.frame(width)
        line_count = scrollable_line_count(frame, max(1, height), self.provider.transcript_state)
        lines = _pad_transcript_lines(frame.transcript_lines, line_count)
        visible_block_ids = self._visible_message_block_ids(self.provider.state_store.snapshot())
        with self._lock:
            anchor = self._pending_reading_anchor
            detail_top = None
            if anchor is None and not self.follow and self._last_reading_frame is not None and frame is not self._last_reading_frame:
                if self._selection_width == width:
                    detail_top = detail_reflow_top(self, self._last_reading_frame, frame)
                if detail_top is None:
                    anchor = capture_reading_anchor(self, self._last_reading_frame)
            if self._selection_width and self._selection_width != width:
                self._selection = None
                self._selection_dragging = False
                self._right_copy_armed = False
            self._selection_width = width
            self._last_lines = lines if isinstance(lines, tuple) else tuple(lines)
            self._line_count = len(lines)
            self._last_render_height = max(1, int(height or 1))
            if detail_top is not None:
                self.cursor_line = min(len(lines) - 1, detail_top + self._last_render_height - 1)
            if anchor is not None and (top := reading_anchor_top(anchor, frame)) is not None:
                self.cursor_line = min(len(lines) - 1, top + self._last_render_height - 1)
                self._pending_reading_anchor = anchor if frame.pending_detail_blocks else None
            self._last_reading_frame = frame
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

    # LLM: 不可聚焦的普通 Window 无法可靠从旧内部滚动值推断新恢复页面的顶行；本回调只根据当前 control 的 typed 锚点与渲染高度计算顶行。
    # 函数用途: 每帧把主/子代理各自的滚动锚点落实到真实 Window，跟随时固定在页底。
    def preferred_vertical_scroll(self, _window: Window) -> int:
        with self._lock:
            line_count = max(1, self._line_count)
            height = max(1, self._last_render_height)
            anchor = max(0, min(line_count - 1, self.cursor_line))
        return max(0, min(line_count - height, anchor - height + 1))

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

    # LLM: 按键路由可以读取既有视口 follow 位，但不能从 footer 文案、光标坐标或未读数量反推该状态。
    # 函数用途: 告诉输入按键层当前正文页是否正在跟随最新消息，不改变滚动位置。
    def is_following(self) -> bool:
        with self._lock:
            return bool(self.follow)

    # LLM: 原滚动/原文边界和follow规则不变；同source复用消息ID，不能在按键路径重扫未变历史。
    # 函数用途: 移动视口并沿原文窗口接续，更新即时未读基线。
    def move(self, delta: int) -> None:
        frame = self.provider.frame(self.provider.last_width)
        frame, top = scroll_detail_window(self, frame, int(delta))
        line_count = scrollable_line_count(frame, self._last_render_height, self.provider.transcript_state)
        visible_block_ids = self._visible_message_block_ids(self.provider.state_store.snapshot())
        with self._lock:
            self._begin_manual_scroll_locked(visible_block_ids)
            self._pending_reading_anchor = None
            self._line_count = line_count
            self._last_reading_frame = frame
            target = max(0, min(self._line_count - 1, top + self._last_render_height - 1))
            self.cursor_line = target
            if int(delta) > 0 and target >= self._line_count - 1 and not frame.detail_page_offsets:
                self.follow = True
                self._unseen_baseline = None
                self._unseen_block_ids = frozenset()
            else:
                self.follow = False
            first_page = not frame.detail_page_offsets or frame.detail_page_offsets[0][0] == 0
            request_older = int(delta) < 0 and top == 0 and first_page
        if request_older and self._older_history is not None:
            self._older_history(False)

    # LLM: home仍离开follow并保原旧页回调，消息基线共用只读memo，不重复扫描历史。
    # 函数用途: 跳到正文顶部并记录当前可计数消息。
    def move_home(self) -> None:
        mode = self.provider.transcript_state
        if mode is not None and mode.snapshot().show_all:
            mode.move_complete_page(-mode.snapshot().complete_page)
        self._last_reading_frame = None
        self._pending_reading_anchor = None
        visible_block_ids = self._visible_message_block_ids(self.provider.state_store.snapshot())
        with self._lock:
            self._begin_manual_scroll_locked(visible_block_ids)
            self.follow = False
            self.cursor_line = 0
        if self._older_history is not None:
            self._older_history(True)

    # LLM: 翻页回调只发读取意图，实际网络及同 owner 身份校验归外层；root/child 不能共享隐式历史游标。
    # 函数用途: 绑定滚到顶部时的异步旧页加载入口。
    def set_older_history_callback(self, callback: Callable[[bool], None]) -> None:
        self._older_history = callback

    # LLM: 预览沿用块内渲染行，完整模式保存来源行；不得用正文匹配或总行数差推测位置。
    # 函数用途: 保存分页之前正在看的确切正文位置，流式尾部增长不影响它。
    def history_anchor(self) -> tuple[str, int] | None:
        frame = self.provider.frame(self.provider.last_width)
        with self._lock:
            if self.follow:
                return None
            line = self.cursor_line
            if frame.line_origins:
                anchor = capture_reading_anchor(self, frame)
                return (anchor.block_id, anchor.line_offset) if anchor else None
        candidates = [(key, offset) for key, offset in frame.block_line_offsets if offset <= line]
        if not candidates:
            return None
        key, offset = candidates[-1]
        return key, line - offset

    # LLM: 原文先定位来源页再恢复；旧页加入已读基线而非新消息，失效选区不能误用于复制。
    # 函数用途: 插入旧历史后保留阅读位置，旧内容不增加未读提示，并清掉失效坐标选区。
    def restore_history_anchor(
        self, anchor: tuple[str, int] | None, *, home: bool = False, block_ids: tuple[str, ...] = (),
    ) -> None:
        state = self.provider.transcript_state
        if state is not None and state.snapshot().show_all and anchor is not None and not home:
            state.focus_complete_block(*anchor)
            self.restore_reading_anchor(ReadingAnchor(*anchor))
        offsets = dict(self.provider.frame(self.provider.last_width).block_line_offsets)
        with self._lock:
            self._selection = None
            if self._unseen_baseline is not None:
                self._unseen_baseline = self._unseen_baseline.union(block_ids)
                self._unseen_block_ids = self._unseen_block_ids.difference(block_ids)
            if not self.follow:
                if home:
                    self.cursor_line = 0
                elif anchor is not None and anchor[0] in offsets:
                    self.cursor_line = offsets[anchor[0]] + anchor[1]

    # LLM: End覆盖此前未重绘的显式跳转，必须清pending锚点；具体末行仍由下一create_content按当前帧决定。
    # 函数用途: 丢弃过期阅读目标，跳到正文底部并继续跟随新输出。
    def move_end(self) -> None:
        with self._lock:
            self._pending_reading_anchor = None
            self.follow = True
            self.cursor_line = max(0, self._line_count - 1)
            self._unseen_baseline = None
            self._unseen_block_ids = frozenset()

    # LLM: 仍按已渲染坐标关闭follow并跳转；复用消息ID memo，不从搜索文本或行数猜消息身份。
    # 函数用途: 将锚点移到指定行并保留即时未读基线。
    def jump_to(self, line_index: int) -> None:
        frame = self.provider.frame(self.provider.last_width)
        visible_block_ids = self._visible_message_block_ids(self.provider.state_store.snapshot())
        with self._lock:
            self._pending_reading_anchor = None
            self._last_reading_frame = frame
            self._line_count = max(1, len(self._last_reading_frame.transcript_lines))
            self._begin_manual_scroll_locked(visible_block_ids)
            self.follow = False
            self.cursor_line = max(0, min(self._line_count - 1, int(line_index or 0)))

    # LLM: 未渲染的快捷切换必须继续携带原锚点，不读取已经失效的旧模式坐标。
    # 函数用途: 读取当前消息位置，快速连按 Ctrl+O/Ctrl+E 也不会丢失来源。
    def reading_anchor(self) -> ReadingAnchor | None:
        frame = self.provider.frame(self.provider.last_width)
        with self._lock:
            return self._pending_reading_anchor or capture_reading_anchor(self, frame)

    # LLM: 显式页快捷键仍按原文页索引工作，但窗口包含前后文，不能错误跳到前一页。
    # 函数用途: 将选中的原文段直接放到视口顶部。
    def jump_to_detail_page(self) -> None:
        frame = self.provider.frame(self.provider.last_width)
        state = self.provider.transcript_state.snapshot()
        top = next((start for page, start, _count in frame.detail_page_offsets if page == state.complete_page), 0)
        self.jump_to(top + self._last_render_height - 1)

    # LLM: 延迟到下一真实布局高度再恢复位置；只影响本视口，不改变另一代理或输入焦点。
    # 函数用途: 为模式切换记录待恢复的阅读位置。
    def restore_reading_anchor(self, anchor: ReadingAnchor | None) -> None:
        with self._lock:
            self.follow = False
            self._pending_reading_anchor = anchor
            self._last_reading_frame = None

    # LLM: current_line 返回 control 自己的结构化锚点，搜索入口无需读取 prompt_toolkit render_info 私有字段。
    # 函数用途: 取得当前 transcript 滚动行。
    def current_line(self) -> int:
        with self._lock:
            return max(0, int(self.cursor_line))

    # LLM: 原follow/typed ID差集口径不变；footer也复用源身份memo，不能成为每帧全扫漏口。
    # 函数用途: 返回跳底提示或新增消息数，同块active转stable不重复计数。
    def scroll_indicator(self) -> str | None:
        visible_block_ids = self._visible_message_block_ids(self.provider.state_store.snapshot())
        with self._lock:
            if self.follow:
                return None
            self._refresh_unseen_locked(visible_block_ids)
            count = len(self._unseen_block_ids)
        if count <= 0:
            return "Jump to bottom"
        noun = "message" if count == 1 else "messages"
        return f"{count} new {noun}"

    # LLM: 读取已由scroll_indicator刷新过的typed未读ID集合，不解析展示字符串。
    # 函数用途: 为独立中文跳底按钮保留未读数量，避免按钮改版丢失新消息提醒。
    def unseen_message_count(self) -> int:
        with self._lock:
            return len(self._unseen_block_ids) if not self.follow else 0

    # LLM: 完整原文按冻结块序/来源行比较，不限四页；pending锚点优先，当前用户首行等号处必须选前一条。
    # 函数用途: 选择视口上方最近一条用户消息，跨窗口和未重绘的连续点击也不丢目标。
    def sticky_user_turn(self) -> TuiUserTurnLine | None:
        rows = self.provider.user_turn_lines()
        frame = self.provider.frame(self.provider.last_width)
        with self._lock:
            anchor = self._pending_reading_anchor
            top = reading_anchor_top(anchor, frame) if anchor is not None else None
            if top is None:
                top = viewport_top(self)
            if frame.detail_page_offsets:
                anchor = anchor or capture_viewport_top_anchor(self, frame)
        if frame.detail_page_offsets:
            if anchor is None or (position := self.provider.complete_block_position(anchor.block_id)) is None:
                return None
            return next((row for row in reversed(rows) if row.block_index < position or (
                row.block_index == position and (anchor.line_offset > 0 or (
                    row.start_line is not None and row.start_line < top
                ))
            )), None)
        return next((row for row in reversed(rows) if row.start_line is not None and row.start_line < top), None)

    # LLM: 仅给独立header供显示与鼠标回调，补齐整行点击区；正文行、选区、搜索及复制来源均不包含这些fragment。
    # 函数用途: 显示上一条用户消息的单行摘要，窄终端也不换行。
    def sticky_user_bar_fragments(self) -> StyleAndTextTuples:
        target = self.sticky_user_turn()
        if target is None:
            return []
        width = self.provider.last_width
        prefix, suffix = "📌 上一条我说的话：", "  [点击跳转到该条 ↑]"
        available = width - display_width_text(prefix + suffix)
        text = (prefix + _single_line_user_summary(target.summary, available) + suffix
                if available > 0 else _single_line_user_summary(prefix + target.summary, width))
        text += " " * max(0, width - display_width_text(text))
        return [("class:tui-new-messages", text, self.sticky_user_bar_mouse_handler)]

    # LLM: 仅header左键释放跳转；完整原文先聚焦目标块所在稀疏页，再恢复source_row/screen_row=0，不排版全历史。
    # 函数用途: 跨四页窗口逐条上跳，立即重选前一条并关闭follow；拖选跨header仍只收口选区。
    def sticky_user_bar_mouse_handler(self, mouse_event: MouseEvent):
        if mouse_event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            return self.mouse_handler(mouse_event)
        if mouse_event.event_type != MouseEventType.MOUSE_UP or mouse_event.button != MouseButton.LEFT:
            return NotImplemented
        if self._finish_selection():
            return None
        target = self.sticky_user_turn()
        if target is None:
            return NotImplemented
        anchor = ReadingAnchor(target.block_id, 0, 0)
        state = self.provider.transcript_state
        if state is not None and state.snapshot().show_all:
            state.focus_complete_block(target.block_id, 0)
        frame = self.provider.frame(self.provider.last_width)
        top = reading_anchor_top(anchor, frame)
        if top is None:
            return NotImplemented
        visible_ids = self._visible_message_block_ids(self.provider.state_store.snapshot())
        with self._lock:
            self._begin_manual_scroll_locked(visible_ids)
            self.follow = False
            self._pending_reading_anchor = anchor
            self._last_reading_frame = frame
            self._line_count = scrollable_line_count(frame, self._last_render_height, self.provider.transcript_state)
            self.cursor_line = min(self._line_count - 1, top + self._last_render_height - 1)
        self.clear_selection()
        self.provider.invalidate()
        return None

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

    # LLM: 松手自动复制是在 Application 创建后挂载的 UI 投影回调；选区坐标与剪贴板传输各自保持独立事实源。
    # 函数用途: 绑定鼠标松手后的复制动作；传入 None 时只保留高亮而不写剪贴板。
    def set_copy_on_select(self, callback: Callable[[str], None] | None) -> None:
        with self._lock:
            self._copy_on_select = callback

    # LLM: 任意终端松手形态、丢失 release 补偿与新按下收口都必须确保一次拖选只触发一次完成；剪贴板回调必须在选区锁外执行以避免 UI 重入。
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
            self.move(-1)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.move(1)
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


# LLM: 保留Window原滚动/焦点接口；独立header仅切分绘制区域，不插入UIContent行或覆盖正文首行，短视口优先正文。
# 类用途: 在普通及详情正文上方放一个可显隐的只读用户消息导航Window。
class _TuiStickyUserWindow(Window):
    # LLM: 仅接收本模块TuiTranscriptControl与显式Window布局参数；header不持第二份状态或正文，鼠标回调仍回到同一control。
    # 函数用途: 创建独立单行header而保持既有Window的滚动、折行、游标与样式配置。
    def __init__(
        self,
        content: TuiTranscriptControl,
        *,
        get_vertical_scroll: Callable[[], int] | None = None,
        wrap_lines: bool = False,
        dont_extend_height: bool = False,
        always_hide_cursor: bool = True,
        allow_scroll_beyond_bottom: bool = False,
        style: str = "",
    ) -> None:
        super().__init__(
            content=content,
            get_vertical_scroll=get_vertical_scroll,
            wrap_lines=wrap_lines,
            dont_extend_height=dont_extend_height,
            always_hide_cursor=always_hide_cursor,
            allow_scroll_beyond_bottom=allow_scroll_beyond_bottom,
            style=style,
        )
        self.sticky_window = Window(
            content=FormattedTextControl(content.sticky_user_bar_fragments, show_cursor=False),
            height=1, wrap_lines=False, always_hide_cursor=True,
            style="class:tui-new-messages",
        )

    # LLM: header属于本Window的只读子控件，向原生layout公开以保持父关系和鼠标分发，不抢输入焦点。
    # 函数用途: 让prompt_toolkit发现独立header控件。
    def get_children(self) -> list[Window]:
        return [self.sticky_window]

    # LLM: 改高度前从最后实际帧捕获正文顶部；首次/resize用新宽度重排，header可见时仅占自身一行，正文坐标仍从0计。
    # 函数用途: 同帧绘制顶栏和下方正文，显隐或高度变化不挤动手动阅读位置。
    def write_to_screen(self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index) -> None:
        if write_position.width <= 0 or write_position.height <= 0:
            return
        control = self.content
        with control._lock:
            anchor = control._pending_reading_anchor
            if not control.follow and anchor is None and control._last_reading_frame is not None:
                anchor = capture_viewport_top_anchor(control, control._last_reading_frame)
        if anchor is not None:
            control.restore_reading_anchor(anchor)
        control.create_content(write_position.width, write_position.height)
        if write_position.height > 1 and control.sticky_user_turn() is not None:
            if anchor is not None:
                control.restore_reading_anchor(anchor)
            body_height = write_position.height - 1
            control.create_content(write_position.width, body_height)
            self.sticky_window.write_to_screen(screen, mouse_handlers,
                WritePosition(write_position.xpos, write_position.ypos, write_position.width, 1),
                parent_style, erase_bg, z_index)
            write_position = WritePosition(write_position.xpos, write_position.ypos + 1, write_position.width, body_height)
        super().write_to_screen(screen, mouse_handlers, write_position, parent_style, erase_bg, z_index)


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
    overlay_provider: TuiFrameProvider
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

    # LLM: 即使正文 provider 正在展示子代理详情页，权限审批浮层仍始终绑定根运行时，确保用户不会因浏览子代理而漏掉待批请求。
    # 函数用途: 返回全局审批面板当前应占的终端行数。
    def overlay_line_count(self) -> int:
        return len(
            self.overlay_provider.frame(
                self.provider.last_width
            ).overlay_lines
        )

    # LLM: 代理页面导航必须作为单一 UI 操作同步切换 provider 及普通/详情两个 control；已访问页面恢复原视口，首次打开的详情页可锚定在提示词顶部而不清空消息或破坏后续贴底恢复。
    # 函数用途: 在主代理和子代理页面之间切换正文，并保留每页位置；首次详情可从提示词顶部打开。
    def set_state_store(
        self,
        state_store: TuiStateStore,
        *,
        start_at_top_if_new: bool = False,
    ) -> None:
        if not isinstance(state_store, TuiStateStore):
            raise TypeError("state_store must be TuiStateStore")
        if self.provider.state_store is state_store:
            return
        self.control.switch_state_store(
            state_store,
            start_at_top_if_new=start_at_top_if_new,
        )
        self.modal_control.switch_state_store(
            state_store,
            start_at_top_if_new=start_at_top_if_new,
        )
        self.provider.set_state_store(state_store)

    # LLM: scroll 委托 control 并由 setup/app invalidate，view 不持有 Application 反向引用。
    # 函数用途: 按行移动 transcript anchor。
    def scroll(self, delta: int) -> None:
        self._active_control().move(delta)

    # LLM: 模式改变先保存原视口的块身份，下一次排版恢复；不把入场解释为回到最新。
    # 函数用途: 在当前阅读位置打开详细视图，保留冻结语义。
    def enter_transcript(self, snapshot: TuiViewSnapshot) -> None:
        anchor = self.control.reading_anchor()
        self.transcript_state.enter(snapshot)
        self.modal_control.restore_reading_anchor(anchor)

    # LLM: 完整视图按当前块定位稀疏索引，收起也传递同一来源锚点；不扫描未加载归档。
    # 函数用途: 原地展开或收起完整内容，快速连续按键保持同一消息位置。
    def toggle_full_detail(self) -> None:
        anchor = self.modal_control.reading_anchor()
        enabled = self.transcript_state.toggle_show_all()
        if enabled and anchor is not None:
            self.transcript_state.focus_complete_block(anchor.block_id, anchor.line_offset)
        self.modal_control.restore_reading_anchor(anchor)

    # LLM: 退出详情恢复当前阅读来源，显式回到最新仍使用独立 end 入口。
    # 函数用途: 收起详细界面后留在刚才读到的消息，用户可以继续输入。
    def exit_transcript(self) -> None:
        anchor = self.modal_control.reading_anchor()
        self.transcript_state.exit()
        self.control.restore_reading_anchor(anchor)

    # LLM: home 委托 control 的结构化锚点，不访问私有 Window scroll 字段。
    # 函数用途: 跳到 transcript 首行。
    def home(self) -> None:
        self._active_control().move_home()

    # LLM: end 恢复 follow-tail，不根据当前显示文本长度计算位置。
    # 函数用途: 跳到 transcript 末尾。
    def end(self) -> None:
        self.transcript_state.exit()
        self.control.move_end()
        self.provider.invalidate()

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

    # LLM: 普通与详情正文模式只暴露一个当前视口 follow 事实；调用方必须经此入口读取，不能直接探入两个 control。
    # 函数用途: 返回当前可见主/子代理正文是否贴着最新消息，供统一按键路由判断。
    def is_following(self) -> bool:
        return self._active_control().is_following()

    # LLM: Ctrl-C 只能读取当前普通/modal viewport 的选区；空选区必须让上层继续走中断或退出语义。
    # 函数用途: 返回当前可见 transcript 的鼠标选中文本。
    def selected_text(self) -> str:
        return self._active_control().selected_text()

    # LLM: 全选与读取选区必须指向同一个 active control；不选另一个代理或隐藏视口，不改会话历史。
    # 函数用途: 将 Ctrl+A 接到当前正文视口，供 Ctrl+C 复制已加载的全部可见内容。
    def select_all(self) -> None:
        self._active_control().select_all()

    # LLM: 清选区委托当前 viewport，不影响另一模式被冻结的消息状态。
    # 函数用途: 清除当前可见 transcript 的选区。
    def clear_selection(self) -> None:
        self._active_control().clear_selection()

    # LLM: 普通与详情两个 transcript control 共享同一个剪贴板投影回调，仅由当前活动视口在自身拖选收口时触发。
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


# LLM: factory仍共享同一provider/store；普通及详情Window各持独立正文外header，原Window滚动/焦点入口不变。
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
    overlay_provider = TuiFrameProvider(state_store, context_factory)
    control = TuiTranscriptControl(provider)
    window = _TuiStickyUserWindow(
        content=control,
        get_vertical_scroll=control.preferred_vertical_scroll,
        wrap_lines=False,
        dont_extend_height=True,
        always_hide_cursor=True,
        allow_scroll_beyond_bottom=False,
        style="class:tui-transcript",
    )
    modal_control = TuiTranscriptControl(provider)
    modal_window = _TuiStickyUserWindow(
        content=modal_control,
        get_vertical_scroll=modal_control.preferred_vertical_scroll,
        wrap_lines=False,
        dont_extend_height=False,
        always_hide_cursor=True,
        allow_scroll_beyond_bottom=False,
        style="class:tui-transcript",
    )
    overlay_control = FormattedTextControl(
        lambda: _flatten_lines(
            overlay_provider.frame(provider.last_width).overlay_lines
        )
    )
    input_status_control = FormattedTextControl(
        lambda: _flatten_lines(provider.frame(provider.last_width).input_status_lines)
    )
    todo_control = FormattedTextControl(
        lambda: _flatten_lines(provider.frame(provider.last_width).todo_lines)
    )
    agent_control = FormattedTextControl(
        lambda: _flatten_lines(provider.frame(provider.last_width).agent_lines)
    )
    # LLM: 根据 mode_state 结构化快照选择普通或详情正文控件，供底栏装饰器读取当前活动视口。
    # 函数用途: 返回当前处于活动状态的 transcript 控件。
    def active_control() -> TuiTranscriptControl:
        return modal_control if mode_state.snapshot().active else control

    footer_control = FormattedTextControl(
        lambda: _decorated_footer(provider, active_control(), control)
    )
    return TuiTranscriptView(
        provider,
        overlay_provider,
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
# 函数用途: 离底或冻结时独立显示中文跳底按钮，保留未读数，完整浏览额外显示页按钮。
def _decorated_footer(
    provider: TuiFrameProvider,
    control: TuiTranscriptControl,
    live_control: TuiTranscriptControl | None = None,
) -> list[tuple[Any, ...]]:
    footer: list[tuple[Any, ...]] = list(provider.frame(provider.last_width).footer)
    indicator = control.scroll_indicator()
    state = provider.transcript_state.snapshot() if provider.transcript_state is not None else None
    if state is not None and state.show_all:
        footer.extend((
            ("", "\n"),
            ("class:tui-new-messages", " [ 上一页 ] ", _detail_page_mouse_handler(provider, control, -1)),
            ("class:tui-new-messages", " [ 下一页 ] ", _detail_page_mouse_handler(provider, control, 1)),
        ))
    if state is not None and state.active:
        indicator = indicator or "frozen"
    if indicator is None:
        return footer
    if footer:
        footer.append(("", "\n"))
    count = control.unseen_message_count()
    footer.append(
        (
            "class:tui-new-messages",
            " ↓ 回到最新 (Ctrl+End) " + (f"· {count} 条新消息" if count else ""),
            _jump_to_bottom_mouse_handler(provider, live_control or control),
        )
    )
    return footer


# LLM: 左键释放退出冻结显示并恢复当前live viewport follow和输入焦点；不修改模型历史或任务状态。
# 函数用途: 点击中文跳底按钮直接回到真正的新消息，不停留在冻结快照末尾。
def _jump_to_bottom_mouse_handler(
    provider: TuiFrameProvider,
    control: TuiTranscriptControl,
) -> Callable[[MouseEvent], Any]:
    # LLM: 仅消费左键释放，不传播任何执行或停止动作。
    # 函数用途: 处理跳底点击，保持当前代理视角。
    def handle(mouse_event: MouseEvent):
        if (
            mouse_event.event_type == MouseEventType.MOUSE_UP
            and mouse_event.button == MouseButton.LEFT
        ):
            if provider.transcript_state is not None:
                provider.transcript_state.exit()
            control.move_end()
            if provider.return_to_input is not None:
                provider.return_to_input()
            provider.invalidate()
            return None
        return NotImplemented

    return handle


# LLM: 翻页点击仅变更当前冻结原文的页号，不能调用历史执行、改变选中代理或写文件。
# 函数用途: 给完整浏览页提供可点击的上一页/下一页，并从新页顶部阅读。
def _detail_page_mouse_handler(provider: TuiFrameProvider, control: TuiTranscriptControl, delta: int):
    # LLM: 仅消费左键释放，页码仍由state边界验证。
    # 函数用途: 切换当前原文页并重置该页滚动位置。
    def handle(mouse_event: MouseEvent):
        if mouse_event.event_type == MouseEventType.MOUSE_UP and mouse_event.button == MouseButton.LEFT:
            if provider.transcript_state is not None and provider.transcript_state.move_complete_page(delta):
                control.jump_to_detail_page()
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


# LLM: 只读取块显示身份与版本，不序列化正文或 metadata；调用方须按不可变容器身份失效缓存。
# 函数用途: 为一组发生变化的显示块建立版本键。
def _block_versions(blocks) -> tuple[tuple[str, int, str], ...]:
    return tuple((block.block_id, block.updated_seq, block.phase) for block in blocks)


# LLM: frame key 使用显示版本字段而非 payload repr；可复用 provider 已核对容器身份的块键，仍独立核对其它可见状态。
# 函数用途: 生成共享 frame 的可哈希版本键，不让底栏变化重新遍历稳定正文。
def _frame_key(
    snapshot: TuiViewSnapshot,
    context: TuiRenderContext,
    transcript_key: tuple[Any, ...] | None = None,
    block_key: tuple[tuple[Any, ...], ...] | None = None,
) -> tuple[Any, ...]:
    blocks = block_key if block_key is not None else (
        _block_versions(snapshot.stable_blocks), _block_versions(snapshot.active_blocks),
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
        snapshot.complete_page,
        snapshot.complete_revision,
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


# LLM: 复制从 formatted line 的可见文本投影生成，直接按源字符索引切片；空选区返回空串，不得中断事件循环或写空剪贴板。
# 函数用途: 提取选中文字并保留跨行换行；未拖选或缩放清空选区后的右键不做复制，中文坐标不重复换算。
def _selected_text(
    lines: tuple[FormattedLine, ...],
    selection: TuiTextSelection | None,
) -> str:
    if selection is None or not lines:
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


# LLM: 布局补空行是纯显示变换，已有足够行时必须返回原 tuple，不能每帧复制稳定历史。
# 函数用途: 按视口需要补空白行；空正文也保留一行，不触碰内容或阅读锚点。
def _pad_transcript_lines(lines: tuple[FormattedLine, ...], line_count: int) -> tuple[FormattedLine, ...]:
    lines = lines or ((),)
    if line_count > len(lines):
        return (*lines, *((),) * (line_count - len(lines)))
    return lines


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
