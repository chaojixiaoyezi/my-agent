"""`/settings` 总览要显示配置告警（2026-09-27，dev 派活）。

背景：参数减量后，已删/未知的配置键会被忽略并记进 `AgentConfig.config_warnings` 或
`CapabilityConfig.config_warnings`，但两个都没有展示点，用户看不到自己的配置里有哪些键没生效。
本测试锁定：`/settings` 与 `/settings all` 的总览里各出现一条“配置告警 N 条”，逐条列出键名和来源；
N 为 0 时这一行不出现；两个来源的告警都能出现。capability 告警来自真实的 capability 配置文件、经统一入口
`capability_config_for_agent` 读取（主配置对象上没有 capability_config，旧写法在生产上永远拿不到）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import settings_control_service as module

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")


def _run(monkeypatch, text: str, config, capability_path, *, home=_ADMIN):
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: home)
    # 与生产同形：capability 配置按 agent 的 capability_config_path 读；文件不存在就是默认实例（没有告警）。
    agent = SimpleNamespace(config=config, capability_config_path=capability_path)
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


@pytest.fixture
def capability_path(tmp_path):
    """capability 配置文件路径；默认不创建，需要告警的用例自己写未知键进去。"""
    return tmp_path / "capability_config.yaml"


def _config(path, *, agent_warnings=()):
    """假主配置：带 config_path 与主配置告警；和 AgentConfig 一样没有 capability_config 属性。"""
    return SimpleNamespace(
        config_path=str(path),
        max_tokens=65536,
        request_timeout=300,
        config_warnings=list(agent_warnings),
    )


def test_overview_shows_both_warning_sources(monkeypatch, config_path, capability_path) -> None:
    config = _config(config_path, agent_warnings=["unknown config key: 'old_key'; ignored"])
    capability_path.write_text("old_capability_key: 1\n", encoding="utf-8")

    text = _run(monkeypatch, "/settings", config, capability_path)

    assert "配置告警 2 条" in text
    assert "old_key" in text
    assert "old_capability_key" in text


def test_overview_marks_the_source_of_each_warning(monkeypatch, config_path, capability_path) -> None:
    """逐条要能看出是 agent 主配置还是 capability 配置来的。"""
    config = _config(config_path, agent_warnings=["unknown config key: 'a'; ignored"])
    capability_path.write_text("b: 1\n", encoding="utf-8")

    text = _run(monkeypatch, "/settings", config, capability_path)

    lines = [line for line in text.splitlines() if "'a'" in line or "'b'" in line]
    assert len(lines) == 2
    assert any("agent" in line or "主配置" in line for line in lines)
    assert any("capability" in line or "能力" in line for line in lines)


def test_overview_hides_the_line_when_there_are_no_warnings(monkeypatch, config_path, capability_path) -> None:
    config = _config(config_path)

    text = _run(monkeypatch, "/settings", config, capability_path)

    assert "配置告警" not in text


def test_all_view_shows_warnings_too(monkeypatch, config_path, capability_path) -> None:
    config = _config(config_path, agent_warnings=["unknown config key: 'stale'; ignored"])

    text = _run(monkeypatch, "/settings all", config, capability_path)

    assert "配置告警 1 条" in text
    assert "stale" in text


def test_all_view_hides_the_line_when_there_are_no_warnings(monkeypatch, config_path, capability_path) -> None:
    config = _config(config_path)

    text = _run(monkeypatch, "/settings all", config, capability_path)

    assert "配置告警" not in text


def test_missing_warning_attributes_do_not_break_the_overview(monkeypatch, config_path, capability_path) -> None:
    """老配置对象没有这些字段时，总览照常出，不能因为取告警而报错。"""
    config = SimpleNamespace(config_path=str(config_path), max_tokens=65536, request_timeout=300)

    text = _run(monkeypatch, "/settings", config, capability_path)

    assert "常用参数" in text
    assert "配置告警" not in text


def test_all_view_shows_capability_file_warnings(monkeypatch, config_path, capability_path) -> None:
    """/settings all 同样显示 capability 文件里没生效的键。"""
    capability_path.write_text("removed_capability_key: 1\n", encoding="utf-8")

    text = _run(monkeypatch, "/settings all", _config(config_path), capability_path)

    assert "配置告警 1 条" in text
    assert "removed_capability_key" in text
