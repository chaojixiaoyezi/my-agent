"""`/settings` 总览要显示配置告警（2026-09-27，dev 派活）。

背景：参数减量后，已删/未知的配置键会被忽略并记进 `AgentConfig.config_warnings` 或
`CapabilityConfig.config_warnings`，但两个都没有展示点，用户看不到自己的配置里有哪些键没生效。
本测试锁定：`/settings` 与 `/settings all` 的总览里各出现一条“配置告警 N 条”，逐条列出键名和来源；
N 为 0 时这一行不出现；两个来源的告警都能出现。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import settings_control_service as module

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")


def _run(monkeypatch, text: str, config, *, home=_ADMIN):
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: home)
    agent = SimpleNamespace(config=config)
    result = module.execute_settings_control(agent, _command(text), None)
    assert result.ok, result.message
    return result.message


def _command(text: str):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.kind == "settings"
    return command


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "user.yaml"
    path.write_text("", encoding="utf-8")
    return path


def _config(path, *, agent_warnings=(), capability_warnings=()):
    """假配置：带 config_path 的主配置 + 可选的 capability 配置投影。"""
    return SimpleNamespace(
        config_path=str(path),
        max_tokens=65536,
        request_timeout=300,
        config_warnings=list(agent_warnings),
        capability_config=SimpleNamespace(config_warnings=list(capability_warnings)),
    )


def test_overview_shows_both_warning_sources(monkeypatch, config_path) -> None:
    config = _config(
        config_path,
        agent_warnings=["unknown config key: 'old_key'; ignored"],
        capability_warnings=["unknown capability config key: 'decision_x_timeout_seconds'; ignored"],
    )

    text = _run(monkeypatch, "/settings", config)

    assert "配置告警 2 条" in text
    assert "old_key" in text
    assert "decision_x_timeout_seconds" in text


def test_overview_marks_the_source_of_each_warning(monkeypatch, config_path) -> None:
    """逐条要能看出是 agent 主配置还是 capability 配置来的。"""
    config = _config(
        config_path,
        agent_warnings=["unknown config key: 'a'; ignored"],
        capability_warnings=["unknown capability config key: 'b'; ignored"],
    )

    text = _run(monkeypatch, "/settings", config)

    lines = [line for line in text.splitlines() if "'a'" in line or "'b'" in line]
    assert len(lines) == 2
    assert any("agent" in line or "主配置" in line for line in lines)
    assert any("capability" in line or "能力" in line for line in lines)


def test_overview_hides_the_line_when_there_are_no_warnings(monkeypatch, config_path) -> None:
    config = _config(config_path)

    text = _run(monkeypatch, "/settings", config)

    assert "配置告警" not in text


def test_all_view_shows_warnings_too(monkeypatch, config_path) -> None:
    config = _config(config_path, agent_warnings=["unknown config key: 'stale'; ignored"])

    text = _run(monkeypatch, "/settings all", config)

    assert "配置告警 1 条" in text
    assert "stale" in text


def test_all_view_hides_the_line_when_there_are_no_warnings(monkeypatch, config_path) -> None:
    config = _config(config_path)

    text = _run(monkeypatch, "/settings all", config)

    assert "配置告警" not in text


def test_missing_warning_attributes_do_not_break_the_overview(monkeypatch, config_path) -> None:
    """老配置对象没有这些字段时，总览照常出，不能因为取告警而报错。"""
    config = SimpleNamespace(config_path=str(config_path), max_tokens=65536, request_timeout=300)

    text = _run(monkeypatch, "/settings", config)

    assert "常用参数" in text
    assert "配置告警" not in text
