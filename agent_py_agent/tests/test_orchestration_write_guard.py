"""LLM: focused tests for orchestration write-target preflight checks.

模块用途: 验证子代理派工前的写入目标预检不会把 UI 文案或 HTML 标签误判成系统路径。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_write_guard import external_write_target_error


# LLM: UI control text and HTML closing tags are content, not filesystem paths.
# 函数用途: `+/-按钮` 和 `</body>` 不应触发越界写入拒绝。
def test_ui_symbols_and_html_tags_do_not_trip_external_write_guard(tmp_path):
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = tmp_path
    mock_agent.subagents.workspace_roots = [tmp_path]

    result = external_write_target_error(
        mock_agent,
        f"在 {tmp_path}/build/cart.html 写购物车页面，数量控件显示 +/-按钮，并在 </body> 前插入脚本。",
        ["write_file"],
    )

    assert result == ""
