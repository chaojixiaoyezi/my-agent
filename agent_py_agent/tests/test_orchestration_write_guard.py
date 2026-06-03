"""LLM: focused tests for orchestration write-target preflight checks.

模块用途: 验证子代理派工前的写入目标预检不会把 UI 文案或 HTML 标签误判成系统路径。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration.write_guard import (
    ExternalWriteTargetRequest,
    external_write_target_error,
)


def _write_target_error(mock_agent, tools: list[str], params: dict[str, object] | None = None) -> str:
    return external_write_target_error(
        ExternalWriteTargetRequest(agent=mock_agent, allowed_tools=tools, params=params or {})
    )


def test_ui_symbols_and_html_tags_do_not_trip_external_write_guard(tmp_path):
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = tmp_path
    mock_agent.subagents.workspace_roots = [tmp_path]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


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


def test_external_write_guard_ignores_url_image_sources(tmp_path):
    workspace_root = tmp_path / "my-claude-code"
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = workspace_root
    mock_agent.subagents.workspace_roots = [workspace_root]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


def test_external_write_guard_ignores_bare_scheme_policy_text(tmp_path):
    workspace_root = tmp_path / "my-claude-code"
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = workspace_root
    mock_agent.subagents.workspace_roots = [workspace_root]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


def test_external_write_guard_ignores_negated_root_route_examples(tmp_path):
    mock_agent = MagicMock()
    mock_agent.subagents.workspace_root = tmp_path
    mock_agent.subagents.workspace_roots = [tmp_path]

    result = _write_target_error(mock_agent, ["write_file"])

    assert result == ""


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
