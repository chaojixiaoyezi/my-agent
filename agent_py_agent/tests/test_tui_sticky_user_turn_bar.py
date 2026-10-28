# LLM: 复用已验证的真实Window/Screen及typed历史夹具；点击后不重绘的按键覆盖须检查最终画面而非仅检查方法调用。
# 模块用途: 覆盖用户顶栏跳转与End、Home和手动导航间的待恢复锚点优先级。
from __future__ import annotations

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from agent_py_agent.tests.test_wave9_tui_bg_watch_and_sticky_user_bar import (
    paint_transcript,
    screen_line,
    sticky_history,
)


# LLM: 使用原生鼠标分发表，点击后的pending锚点尚未布局前就执行导航，不用两次点击之间的create_content掩盖问题。
# 函数用途: 验证顶栏跳到U3后立即End，再真实重绘仍跟随页底且顶栏重选U3。
@pytest.mark.parametrize("mode", ["normal", "detail", "complete"])
def test_sticky_click_then_end_before_redraw_follows_latest(mode):
    view, store, _seq = sticky_history()
    view.control.create_content(80, 8)
    modal = mode != "normal"
    if modal:
        view.enter_transcript(store.snapshot())
        if mode == "complete":
            view.toggle_full_detail()
    control = view.modal_control if modal else view.control
    app, screen, handlers = paint_transcript(view, modal=modal)
    assert control.sticky_user_turn().block_id == "u-3"
    with set_app(app):
        handlers.mouse_handlers[0][2](MouseEvent(Point(2, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))
    assert control._pending_reading_anchor.block_id == "u-3"
    control.move_end()
    app, screen, _handlers = paint_transcript(view, modal=modal, app=app)
    assert control.is_following() is True
    assert control._pending_reading_anchor is None
    assert control.cursor_line == control._line_count - 1
    assert control.sticky_user_turn().block_id == "u-3"
    assert "上一条我说的话" in screen_line(screen, 0) and "提问 3" in screen_line(screen, 0)


# LLM: 现有显式手动导航必须同样覆盖pending；守卫已有清理逻辑，不为测试新增导航实现。
# 函数用途: 确认Home、移动及指定行跳转不会在重绘时恢复过期顶栏目标。
@pytest.mark.parametrize("action", ["home", "move", "jump"])
def test_manual_navigation_clears_pending_sticky_anchor(action):
    view, _store, _seq = sticky_history()
    app, _screen, handlers = paint_transcript(view)
    with set_app(app):
        handlers.mouse_handlers[0][2](MouseEvent(Point(2, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))
    control = view.control
    assert control._pending_reading_anchor is not None
    if action == "home":
        control.move_home()
    elif action == "move":
        control.move(-1)
    else:
        control.jump_to(3)
    assert control._pending_reading_anchor is None
    paint_transcript(view, app=app)
    assert control._pending_reading_anchor is None and not control.is_following()
