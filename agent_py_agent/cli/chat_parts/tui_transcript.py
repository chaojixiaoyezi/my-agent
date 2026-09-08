# LLM: 本模块是 Ctrl-O transcript 模式、冻结快照与全文搜索的唯一 UI 状态源；切代理必须重绑显示快照，不写会话或运行状态。
# 模块用途: 提供详细 transcript、less 式搜索和冻结正文；当前客户端的故障提示不能被模式说明覆盖。

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from .tui_block_renderer import TuiRenderFrame
from .tui_markdown import FormattedLine, display_width_text, fragments_text
from .tui_view_model import TuiViewSnapshot


# LLM: TuiTranscriptSearchMatch 只保存当前宽度下的可见行坐标；resize 后必须重建，不能当持久会话引用。
# 类用途: 描述 transcript 中一个可导航的搜索命中。
@dataclass(frozen=True)
class TuiTranscriptSearchMatch:
    line_index: int
    start: int
    end: int


# LLM: TuiTranscriptModeSnapshot 只暴露布局、footer 和 keybinding 所需状态；冻结的完整 view snapshot 不对 renderer 外泄。
# 类用途: 一次性读取 transcript 模式、搜索词和当前命中计数。
@dataclass(frozen=True)
class TuiTranscriptModeSnapshot:
    active: bool = False
    show_all: bool = False
    search_open: bool = False
    search_query: str = ""
    match_count: int = 0
    current_match: int = 0


# LLM: TuiTranscriptModeState 在同一锁内维护模式和搜索；冻结 snapshot 只属于当前显示来源，control 动作仍由外层 keybinding 调用。
# 类用途: 管理 Ctrl-O 进入/退出、Ctrl-E、`/` 搜索、n/N 导航与 resize 清理。
class TuiTranscriptModeState:
    # LLM: invalidate callback 只能丢 frame cache 并请求重绘，不能从 worker 线程操作 prompt_toolkit layout。
    # 函数用途: 创建一个关闭状态的 transcript 控制器。
    def __init__(self, invalidate: Callable[[], None] | None = None) -> None:
        self._active = False
        self._show_all = False
        self._search_open = False
        self._committed_query = ""
        self._editing_query = ""
        self._query_before_search = ""
        self._anchor_line = 0
        self._matches: tuple[TuiTranscriptSearchMatch, ...] = ()
        self._matches_query = ""
        self._current_match_index = -1
        self._render_width = 0
        self._frozen_snapshot: TuiViewSnapshot | None = None
        self._invalidate = invalidate
        self._lock = threading.RLock()

    # LLM: callback 替换不累积订阅；provider 建立后用它接管 redraw。
    # 函数用途: 绑定当前 frame provider 的失效入口。
    def set_invalidate_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._invalidate = callback

    # LLM: snapshot 不返回搜索坐标或冻结正文，避免 footer cache key复制大对象。
    # 函数用途: 返回当前 transcript 交互状态。
    def snapshot(self) -> TuiTranscriptModeSnapshot:
        with self._lock:
            query = self._editing_query if self._search_open else self._committed_query
            current = self._current_match_index + 1 if self._matches else 0
            return TuiTranscriptModeSnapshot(
                active=self._active,
                show_all=self._show_all,
                search_open=self._search_open,
                search_query=query,
                match_count=len(self._matches),
                current_match=current,
            )

    # LLM: enter 冻结一次不可变 view snapshot；后续 live 事件继续进 canonical store，但不会改写本次 transcript 视图。
    # 函数用途: 进入详细 transcript 并返回是否发生状态变化。
    def enter(self, snapshot: TuiViewSnapshot) -> bool:
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._show_all = False
            self._frozen_snapshot = snapshot
            self._reset_search_locked()
        self._notify()
        return True

    # LLM: exit 丢弃冻结投影与所有 less 搜索状态；不会删除 store 中在查看期间到达的新事件。
    # 函数用途: 退出详细 transcript 并返回是否发生状态变化。
    def exit(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._active = False
            self._show_all = False
            self._frozen_snapshot = None
            self._reset_search_locked()
        self._notify()
        return True

    # LLM: toggle 只在关闭时读取调用方给出的同一时刻 live snapshot，不能自行访问全局 store。
    # 函数用途: 切换 transcript 模式并返回切换后的 active 值。
    def toggle(self, snapshot: TuiViewSnapshot) -> bool:
        if self.snapshot().active:
            self.exit()
            return False
        self.enter(snapshot)
        return True

    # LLM: snapshot_for_render 在 active 时始终返回入场冻结对象；缺失冻结对象属于接线错误并显式失败。
    # 函数用途: 选择当前 frame 应渲染的 live 或 frozen view snapshot。
    def snapshot_for_render(self, live_snapshot: TuiViewSnapshot) -> TuiViewSnapshot:
        with self._lock:
            if not self._active:
                return live_snapshot
            if self._frozen_snapshot is None:
                raise RuntimeError("active TUI transcript is missing frozen snapshot")
            return self._frozen_snapshot

    # LLM: A typed view-source change replaces only the display snapshot. Keep modal/show-all
    # mode, reset source-specific search coordinates, and never carry another agent's frozen rows.
    # 函数用途: 详细模式切回父级时换成父级正文并重新冻结，避免操作目标变了而画面还停在子代理。
    def rebind_view_snapshot(self, snapshot: TuiViewSnapshot) -> None:
        with self._lock:
            if not self._active:
                return
            self._frozen_snapshot = snapshot
            self._reset_search_locked()
        self._notify()

    # LLM: 冻结阅读可追加更早的静态块，但不能夹入查看期间的新回复或改变原活动/审批快照。
    # 函数用途: Ctrl+O 模式翻旧页时保留当前阅读现场，同时补齐新读到的历史前缀。
    def prepend_history(self, live_snapshot: TuiViewSnapshot, block_ids: tuple[str, ...]) -> None:
        with self._lock:
            frozen = self._frozen_snapshot
            if not self._active or frozen is None:
                return
            ids = set(block_ids)
            old_ids = {block.block_id for block in frozen.stable_blocks}
            prefix = [block for block in live_snapshot.stable_blocks if block.block_id in ids - old_ids]
            welcome = {block.block_id: block for block in live_snapshot.stable_blocks if block.kind == "session_started"}
            self._frozen_snapshot = replace(frozen, stable_blocks=(
                *prefix, *(welcome.get(block.block_id, block) for block in frozen.stable_blocks),
            ))
            self._matches, self._matches_query = (), ""
        self._notify()

    # LLM: show-all 仅在 transcript 模式可切换；普通聊天视图没有隐藏的第二份开关状态。
    # 函数用途: 切换详细 transcript 的完整展示档位。
    def toggle_show_all(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._show_all = not self._show_all
            enabled = self._show_all
        self._notify()
        return enabled

    # LLM: open_search 保存进入搜索前的 committed query 与滚动锚点；编辑框总从空串开始，匹配 less 的 `/` 习惯。
    # 函数用途: 打开 transcript 搜索栏。
    def open_search(self, anchor_line: int) -> None:
        with self._lock:
            if not self._active or self._search_open:
                return
            self._search_open = True
            self._query_before_search = self._committed_query
            self._editing_query = ""
            self._anchor_line = max(0, int(anchor_line or 0))
            self._matches = ()
            self._matches_query = ""
            self._current_match_index = -1
        self._notify()

    # LLM: query 只是可见全文筛选条件；正文不触发命令、状态变化或业务搜索。
    # 函数用途: 增量更新 transcript 搜索词。
    def update_search_query(self, query: str) -> None:
        with self._lock:
            if not self._search_open:
                return
            self._editing_query = str(query or "")
            self._matches_query = ""
        self._notify()

    # LLM: commit 仅在已有匹配时持久化 query；零匹配关闭搜索并清空导航状态。
    # 函数用途: 用 Enter 接受 transcript 搜索并返回当前命中行。
    def commit_search(self) -> int | None:
        with self._lock:
            if not self._search_open:
                return self._current_line_locked()
            self._committed_query = self._editing_query if self._matches else ""
            self._search_open = False
            self._editing_query = ""
            if not self._committed_query:
                self._matches = ()
                self._matches_query = ""
                self._current_match_index = -1
            line = self._current_line_locked()
        self._notify()
        return line

    # LLM: cancel 恢复 `/` 之前 committed query 与滚动锚点，临时 query 的匹配和高亮不能泄漏。
    # 函数用途: 用 Esc/Ctrl-C/Ctrl-G 取消 transcript 搜索并返回原锚点。
    def cancel_search(self) -> int:
        with self._lock:
            self._search_open = False
            self._committed_query = self._query_before_search
            self._editing_query = ""
            self._matches = ()
            self._matches_query = ""
            self._current_match_index = -1
            anchor = self._anchor_line
        self._notify()
        return anchor

    # LLM: navigate 只在已计算匹配中循环索引；n/N 不改变 query 或正文。
    # 函数用途: 跳到下一个或上一个 transcript 搜索命中并返回目标行。
    def navigate(self, *, reverse: bool = False) -> int | None:
        with self._lock:
            if not self._matches:
                return None
            delta = -1 if reverse else 1
            self._current_match_index = (
                self._current_match_index + delta
            ) % len(self._matches)
            line = self._current_line_locked()
        self._notify()
        return line

    # LLM: 只装饰格式化行和搜索结果；preserve_footer 来自实时 typed context，不能解析提示文案或冻结正文判断健康。
    # 函数用途: 添加搜索高亮与模式说明；有高优先级实时提示时保留原底栏，不清除搜索状态。
    def decorate_frame(
        self, frame: TuiRenderFrame, *, width: int, preserve_footer: bool = False,
    ) -> TuiRenderFrame:
        normalized_width = max(1, int(width or 1))
        with self._lock:
            if not self._active:
                return frame
            if self._render_width and self._render_width != normalized_width:
                self._committed_query = ""
                self._editing_query = ""
                self._search_open = False
                self._matches = ()
                self._matches_query = ""
                self._current_match_index = -1
            self._render_width = normalized_width
            query = self._editing_query if self._search_open else self._committed_query
            matches = _find_transcript_matches(frame.transcript_lines, query)
            if query != self._matches_query or matches != self._matches:
                self._matches = matches
                self._matches_query = query
                self._current_match_index = _nearest_match_index(
                    matches,
                    self._anchor_line,
                )
            current_index = self._current_match_index
            search_open = self._search_open
            show_all = self._show_all
        highlighted = _highlight_transcript_lines(
            frame.transcript_lines,
            matches,
            current_index=current_index,
        )
        footer = frame.footer if preserve_footer else _transcript_footer(
            normalized_width,
            show_all=show_all,
            search_open=search_open,
            match_count=len(matches),
            current_match=current_index + 1 if matches else 0,
        )
        return replace(frame, transcript_lines=highlighted, footer=footer)

    # LLM: current_match_line 只返回已渲染宽度下的当前位置；调用前若 frame 未计算则返回 None。
    # 函数用途: 取得当前搜索命中的 transcript 行。
    def current_match_line(self) -> int | None:
        with self._lock:
            return self._current_line_locked()

    # LLM: 内部 reset 必须在已持锁上下文调用，并清理所有 query/match/width 状态。
    # 函数用途: 复位一次 less transcript 会话。
    def _reset_search_locked(self) -> None:
        self._search_open = False
        self._committed_query = ""
        self._editing_query = ""
        self._query_before_search = ""
        self._anchor_line = 0
        self._matches = ()
        self._matches_query = ""
        self._current_match_index = -1
        self._render_width = 0

    # LLM: line 查询只看结构化 match 索引，越界时不得猜首尾行。
    # 函数用途: 返回锁内当前搜索命中行。
    def _current_line_locked(self) -> int | None:
        if not self._matches or not 0 <= self._current_match_index < len(self._matches):
            return None
        return self._matches[self._current_match_index].line_index

    # LLM: redraw callback 在锁外调用，避免 provider.frame 重入本状态锁。
    # 函数用途: 通知 frame provider transcript 状态已变化。
    def _notify(self) -> None:
        with self._lock:
            callback = self._invalidate
        if callback is not None:
            callback()


# LLM: 搜索只索引实际可见 formatted lines 并使用 casefold；隐藏 metadata、模型上下文和未渲染 thinking 都不产生 phantom match。
# 函数用途: 找出当前宽度下所有 transcript 可见命中。
def _find_transcript_matches(
    lines: tuple[FormattedLine, ...],
    query: str,
) -> tuple[TuiTranscriptSearchMatch, ...]:
    needle = str(query or "").casefold()
    if not needle:
        return ()
    matches: list[TuiTranscriptSearchMatch] = []
    for line_index, line in enumerate(lines):
        text = fragments_text(line)
        folded = text.casefold()
        offset = 0
        while True:
            start = folded.find(needle, offset)
            if start < 0:
                break
            end = start + len(needle)
            matches.append(TuiTranscriptSearchMatch(line_index, start, end))
            offset = max(end, start + 1)
    return tuple(matches)


# LLM: 首次搜索从进入 `/` 时的滚动锚点选最近命中，避免总跳到长会话首条结果。
# 函数用途: 选择距锚点最近的 match 索引。
def _nearest_match_index(
    matches: tuple[TuiTranscriptSearchMatch, ...],
    anchor_line: int,
) -> int:
    if not matches:
        return -1
    anchor = max(0, int(anchor_line or 0))
    return min(
        range(len(matches)),
        key=lambda index: (abs(matches[index].line_index - anchor), matches[index].line_index),
    )


# LLM: 高亮切分基于每行可见字符 offset 并保留原 fragment style；当前命中与普通命中使用不同附加 class。
# 函数用途: 将搜索匹配覆盖到 transcript formatted lines。
def _highlight_transcript_lines(
    lines: tuple[FormattedLine, ...],
    matches: tuple[TuiTranscriptSearchMatch, ...],
    *,
    current_index: int,
) -> tuple[FormattedLine, ...]:
    by_line: dict[int, list[tuple[int, int, bool]]] = {}
    for index, match in enumerate(matches):
        by_line.setdefault(match.line_index, []).append(
            (match.start, match.end, index == current_index)
        )
    decorated: list[FormattedLine] = []
    for line_index, line in enumerate(lines):
        ranges = by_line.get(line_index)
        decorated.append(_highlight_line(line, ranges or []))
    return tuple(decorated)


# LLM: 单行切分按原 fragment 的 codepoint offset 处理，多个非重叠 match 依序附加搜索 style。
# 函数用途: 高亮一行中的指定字符区间。
def _highlight_line(
    line: FormattedLine,
    ranges: list[tuple[int, int, bool]],
) -> FormattedLine:
    if not ranges:
        return line
    result: list[tuple[str, str]] = []
    absolute = 0
    range_index = 0
    for style, text in line:
        local = 0
        while local < len(text):
            while range_index < len(ranges) and ranges[range_index][1] <= absolute + local:
                range_index += 1
            if range_index >= len(ranges):
                result.append((style, text[local:]))
                local = len(text)
                continue
            start, end, current = ranges[range_index]
            fragment_end = absolute + len(text)
            if start >= fragment_end:
                result.append((style, text[local:]))
                local = len(text)
                continue
            if start > absolute + local:
                cut = start - absolute
                result.append((style, text[local:cut]))
                local = cut
                continue
            cut = min(len(text), end - absolute)
            extra = "class:tui-search-current" if current else "class:tui-search-match"
            result.append(((style + " " + extra).strip(), text[local:cut]))
            local = cut
        absolute += len(text)
    return tuple(result)


# LLM: transcript footer 只由模式/search 结构化字段生成；宽度不足时直接截断，不改变 keybinding 行为。
# 函数用途: 生成详细 transcript 底栏和右侧搜索计数。
def _transcript_footer(
    width: int,
    *,
    show_all: bool,
    search_open: bool,
    match_count: int,
    current_match: int,
) -> FormattedLine:
    suffix = " · ctrl+e to collapse" if show_all else " · ctrl+e to show all"
    if match_count and not search_open:
        suffix = " · n/N to navigate"
    left = "  Showing detailed transcript · ctrl+o to toggle" + suffix
    right = f"{current_match}/{match_count}  " if match_count else ""
    room = max(0, int(width) - display_width_text(right))
    clipped_left = left[:room]
    padding = max(0, room - display_width_text(clipped_left))
    return (("class:tui-muted", clipped_left + " " * padding + right),)


__all__ = [
    "TuiTranscriptModeSnapshot",
    "TuiTranscriptModeState",
    "TuiTranscriptSearchMatch",
]
