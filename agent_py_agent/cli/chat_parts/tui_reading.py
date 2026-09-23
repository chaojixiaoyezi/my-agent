# LLM: 阅读位置只来自显示块身份、来源行与视口坐标；不写模型历史，不猜正文或任务状态。
# 模块用途: 统一普通、详细和完整原文的阅读定位，并把内部分页接成连续滚动。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# LLM: 锚点属于当前显示来源，切换代理必须丢弃；screen_row 保留消息在屏幕上的位置。
# 类用途: 记录正在阅读的消息及其中的位置，不依赖会随展开变化的全局行号。
@dataclass(frozen=True)
class ReadingAnchor:
    block_id: str
    line_offset: int = 0
    screen_row: int = 0


# LLM: 从当前渲染锚点换算视口顶部，滚动操作必须移动此坐标而非隐藏光标。
# 函数用途: 统一计算实际显示的首行，短内容固定从首行显示。
def viewport_top(control: Any) -> int:
    return max(0, min(control._line_count - control._last_render_height,
                      control.cursor_line - control._last_render_height + 1))


# LLM: 同宽原文页的内容不可变，异步邻页只能改变其窗口偏移；按页内显示行恢复，不把长逻辑行归零。
# 函数用途: 邻页加载后保留超长换行文本内正在阅读的那一行，仅供同展示模式的重绘使用。
def detail_reflow_top(control: Any, previous: Any, current: Any) -> int | None:
    top = viewport_top(control)
    pages = [(page, start) for page, start, _count in previous.detail_page_offsets if start <= top]
    if not pages or not current.detail_page_offsets:
        return None
    page, start = pages[-1]
    offset = next((offset for index, offset, _count in current.detail_page_offsets if index == page), None)
    return None if offset is None else offset + top - start


# LLM: 仍有后页时允许当前窗口末行到达视口顶部，空白仅用于布局，不进入原文、搜索或复制来源。
# 函数用途: 避免几个短页合起来不够一屏时，终端的页底限制让下一原文页永远无法触达。
def scrollable_line_count(frame: Any, height: int, state: Any) -> int:
    count = max(1, len(frame.transcript_lines))
    if frame.detail_page_offsets and state is not None:
        if frame.detail_page_offsets[-1][0] + 1 < state.snapshot().complete_page_count:
            return count + max(0, height - 1)
    return count


# LLM: 优先锚定屏内首个块边界；长块内部用来源行，不能按文字相似度寻找展开位置。
# 函数用途: 保存当前可见消息和屏幕行，供模式切换、换行与归档加载后恢复。
def capture_reading_anchor(control: Any, frame: Any) -> ReadingAnchor | None:
    top, height = viewport_top(control), control._last_render_height
    if frame.line_origins:
        if top >= len(frame.line_origins):
            return None
        candidates = [(index, origin) for index, origin in enumerate(frame.line_origins[top:top + height], top)
                      if origin[0] and origin[1] == 0]
        index, (key, row) = candidates[0] if candidates else (top, frame.line_origins[top])
        return ReadingAnchor(key, row, index - top) if key else None
    starts = frame.block_line_offsets
    visible = [(key, start) for key, start in starts if top <= start < top + height]
    if visible:
        key, start = visible[0]
        return ReadingAnchor(key, 0, start - top)
    previous = [(key, start) for key, start in starts if start <= top]
    if previous:
        key, start = previous[-1]
        return ReadingAnchor(key, top - start)
    return None


# LLM: 展开原文按来源行恢复，预览按块内可见范围夹取；不允许越过当前块落到另一条消息。
# 函数用途: 把保存的消息锚点换算为新画面的顶部位置。
def reading_anchor_top(anchor: ReadingAnchor, frame: Any) -> int | None:
    if frame.line_origins:
        candidates = [(abs(row - anchor.line_offset), index) for index, (key, row) in enumerate(frame.line_origins)
                      if key == anchor.block_id]
        if candidates:
            return max(0, min(candidates)[1] - anchor.screen_row)
        return None
    starts = frame.block_line_offsets
    for index, (key, start) in enumerate(starts):
        if key == anchor.block_id:
            end = starts[index + 1][1] if index + 1 < len(starts) else len(frame.transcript_lines)
            return max(0, start + min(anchor.line_offset, max(0, end - start - 1)) - anchor.screen_row)
    return None


# LLM: 只移动既有原文页索引，页面交换后用页内位置恢复；远端加载仍归 transcript state。
# 函数用途: 滚动越过内部页边界时接续相邻内容，返回当前窗口中的新顶部。
def scroll_detail_window(control: Any, frame: Any, delta: int) -> tuple[Any, int]:
    state = control.provider.transcript_state
    requested_top = viewport_top(control) + delta
    top = max(0, requested_top)
    pages = frame.detail_page_offsets
    if not pages or state is None:
        return frame, top
    candidates = [(page, start) for page, start, _count in pages if start <= top]
    page, start = candidates[-1] if candidates else (pages[0][0], pages[0][1])
    current = state.snapshot()
    previous_page = delta < 0 and requested_top < 0 and pages[0][0] > 0
    if previous_page:
        page, start = pages[0][0] - 1, 0
        top = 0
    if page != current.complete_page and state.move_complete_page(page - current.complete_page):
        refreshed = control.provider.frame(control.provider.last_width)
        offsets = {index: offset for index, offset, _count in refreshed.detail_page_offsets}
        top = offsets.get(page, 0) + max(0, top - start)
        if previous_page:
            count = next(count for index, _offset, count in refreshed.detail_page_offsets if index == page)
            top = max(0, offsets[page] + count + requested_top)
        return refreshed, top
    return frame, top
