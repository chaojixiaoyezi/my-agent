"""LLM: focused tests for orchestration write-target preflight checks.

模块用途: 验证子代理派工前的写入目标预检不会把 UI 文案或 HTML 标签误判成系统路径。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_write_guard import (
    ExternalWriteTargetRequest,
    external_write_target_error,
)


# LLM: _write_target_error keeps tests focused on structured params instead of prose parsing.
# 函数用途: 用测试 agent、工具和机器参数调用写入目标预检。
def _write_target_error(mock_agent, tools: list[str], params: dict[str, object] | None = None) -> str:
    return external_write_target_error(
        ExternalWriteTargetRequest(agent=mock_agent, allowed_tools=tools, params=params or {})
    )


# LLM: UI control text and HTML closing tags are content, not filesystem paths.
# 函数用途: `+/-按钮` 和 `</body>` 不应触发越界写入拒绝。
def test_ui_symbols_and_html_tags_do_not_trip_external_write_guard(tmp_path):
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = tmp_path
    mock_agent.subagents.workspace_roots = [tmp_path]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


# LLM: A near-miss absolute path is no longer blocked by a workspace allowlist.
# 函数用途: 验证普通外部目标不会被派工守卫误当成权限问题。
def test_external_write_guard_allows_non_dangerous_external_targets():
    workspace_root = "/Users/example/my-claude-code"
    wrong_target = "/Users/other-user/my-claude-code/deliverables/shop/build"
    suggested_target = "/Users/example/my-claude-code/deliverables/shop/build"
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = workspace_root
    mock_agent.subagents.workspace_roots = [workspace_root]

    result = _write_target_error(mock_agent, ["write_file"], {"extra_write_roots": [wrong_target]})

    assert suggested_target
    assert result == ""


# LLM: URL image sources are content references, not write targets.
# 函数用途: 验证条目图片 URL 不会被派工写入预检误切成 `s://...` 并阻断 child 创建。
def test_external_write_guard_ignores_url_image_sources(tmp_path):
    workspace_root = tmp_path / "my-claude-code"
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = workspace_root
    mock_agent.subagents.workspace_roots = [workspace_root]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


# LLM: Bare scheme text such as "no http:// links" is content policy wording, not a Windows path.
# 函数用途: 覆盖真实 E2E 中 `无 http:// 外链图片` 被误切成 `p://` 后阻断 QA 子代理创建的问题。
def test_external_write_guard_ignores_bare_scheme_policy_text(tmp_path):
    workspace_root = tmp_path / "my-claude-code"
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = workspace_root
    mock_agent.subagents.workspace_roots = [workspace_root]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


# LLM: Negative route examples should not be interpreted as external filesystem write targets.
# 函数用途: 覆盖真实自然语言 E2E：`不要写 /collections` 是链接规则示例，不是要写到系统根目录。
def test_external_write_guard_ignores_negated_root_route_examples(tmp_path):
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = tmp_path
    mock_agent.subagents.workspace_roots = [tmp_path]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


# LLM: Dangerous external output targets are still blocked after route-example filtering.
# 函数用途: 确认写入守卫只拒绝危险目录，不再把普通外部目录当权限问题。
def test_external_write_guard_blocks_configured_dangerous_targets(tmp_path):
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = tmp_path
    mock_agent.subagents.workspace_roots = [tmp_path]
    mock_agent.config.path_access_mode = "normal"
    mock_agent.config.path_dangerous_roots = [str(tmp_path / "danger")]

    result = _write_target_error(
        mock_agent,
        ["write_file"],
        {"output_refs": [str(tmp_path / "danger" / "index.html")]},
    )

    assert "危险目录" in result
