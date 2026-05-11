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


# LLM: A near-miss absolute path should teach the coordinator to retry, not ask for wider permissions.
# 函数用途: 验证用户名拼错但工作区尾部一致时，派工守卫会给出可直接重试的修正路径提示。
def test_external_write_guard_suggests_workspace_typo_retry():
    workspace_root = "/Users/example/my-claude-code"
    wrong_target = "/Users/other-user/my-claude-code/deliverables/shop/build"
    suggested_target = "/Users/example/my-claude-code/deliverables/shop/build"
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = workspace_root
    mock_agent.subagents.workspace_roots = [workspace_root]

    result = external_write_target_error(
        mock_agent,
        f"创建页面到 {wrong_target}，要求 index.html 和 app.js。",
        ["write_file"],
    )

    assert "suspected_path_typo=true" in result
    assert f"target={wrong_target}" in result
    assert f"suggested_target={suggested_target}" in result
    assert "请使用 suggested_target 重新调用 schedule_child_subagents" in result
    assert "不要写 capability_request" in result
