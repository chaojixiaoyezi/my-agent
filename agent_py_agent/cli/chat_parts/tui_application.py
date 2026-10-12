# LLM: TUI 重绘调度只使用单调时钟；沿 prompt_toolkit 3.0 的单次 invalidate 和原渲染生命周期，不修改全局时间或供应商代码。
# 模块用途: 避免系统校时向后跳变时输入已收到但屏幕几十秒不刷；修改须核对真实 PTY 和计时回归。
from __future__ import annotations

import time

from prompt_toolkit.application import Application


# LLM: 只接管重绘触发的计时，输入、终端 raw 生命周期、后台任务和 renderer 仍由父类负责；依赖 3.0 的生命周期字段。
# 类用途: 为聊天界面保留帧率合并，同时保证等待间隔不受系统时间校正影响。
class TuiApplication(Application):
    # LLM: 单调时间基线仅属于当前 Application，不持久化、不复用墙钟的 _last_redraw_time。
    # 函数用途: 创建终端应用并初始化最近一次实际重绘的单调时间。
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._tui_last_redraw_monotonic = 0.0

    # LLM: 可从 worker 调用；先合并 invalidate，再在原事件循环定时执行，不阻塞输入，退出时由父类任务生命周期清理。
    # 函数用途: 在最多一个帧间隔后安排重绘，避免墙钟倒退让 _invalidated 长时间不能清除。
    def invalidate(self) -> None:
        loop = self.loop
        if not self._is_running or loop is None or loop.is_closed() or self._invalidated:
            return
        self._invalidated = True
        loop.call_soon_threadsafe(self.on_invalidate.fire)

        # LLM: 回调只在所属 UI loop 调用；清合并标志后走父类渲染，避免新事件失去重绘机会。
        # 函数用途: 执行一次已经合并的重绘请求。
        def redraw() -> None:
            self._invalidated = False
            self._redraw()

        # LLM: 使用 loop.call_later 的单调调度；不沿父类使用墙钟的 postpone deadline，也不循环检查私有 ready 队列。
        # 函数用途: 在 UI 线程安排最多一个帧间隔的非阻塞刷新。
        def schedule() -> None:
            wait = max(0.0, float(self.min_redraw_interval or 0.0) -
                       (time.monotonic() - self._tui_last_redraw_monotonic))
            loop.call_later(wait, redraw)

        loop.call_soon_threadsafe(schedule)

    # LLM: 只有父类确实渲染才推进本地计时；子应用/终端借用期间不假造帧，结束时仍由原逻辑恢复输入。
    # 函数用途: 原样执行终端渲染，并记录实际帧的单调时钟基线。
    def _redraw(self, render_as_done: bool = False) -> None:
        before = self.render_counter
        super()._redraw(render_as_done=render_as_done)
        if self.render_counter != before:
            self._tui_last_redraw_monotonic = time.monotonic()
