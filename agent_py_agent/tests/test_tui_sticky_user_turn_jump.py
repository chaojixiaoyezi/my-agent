# LLM: 任务指定名称与本树既有文件名不同；显式复用原测试及其autouse清理夹具，不删改断言或制造空门禁。
# 模块用途: 为第四轮固定命令提供兼容入口，测试与夹具仍只在原模块维护。
from agent_py_agent.tests.test_tui_sticky_user_turn_bar import (
    test_manual_navigation_clears_pending_sticky_anchor as test_manual_navigation_clears_pending_sticky_anchor,
)
from agent_py_agent.tests.test_tui_sticky_user_turn_bar import (
    test_sticky_click_then_end_before_redraw_follows_latest as test_sticky_click_then_end_before_redraw_follows_latest,
)
