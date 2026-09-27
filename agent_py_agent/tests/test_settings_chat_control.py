"""聊天 `/settings`：在 TUI 与 IM 里查找、查看、修改、恢复默认、回滚参数（参数中心，2026-09-27）。

背景：用户几乎不用命令行，并希望 my-agent 与自己都能改更多参数、改错能回滚。本测试锁定：解析拒绝式校验且 set 的值保留原样、
命令目录走 Gateway、TUI 文本还原与本地模式拒绝、Gateway 分派不落入 steer/stop、只有管理员可用（非管理员看不到任何参数）、
读写走参数中心并记账、边界项拒绝、普通异常不承诺“没有改动”。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.command_catalog import match_conversation_command
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import settings_control_service as module
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.cli.chat_parts import control_runtime

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
_FEISHU_USER = SimpleNamespace(owner_provider="feishu", owner_kind="users", owner_id="ou_x")


def _settings(text: str) -> ConversationControlCommand:
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.kind == "settings"
    return command


def _run(monkeypatch, path, text: str, *, home=_ADMIN):
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: home)
    agent = SimpleNamespace(config=SimpleNamespace(config_path=str(path), max_tokens=65536))
    return module.execute_settings_control(agent, _settings(text), None)


@pytest.fixture
def user_config(tmp_path):
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "myagent"\nrequest_timeout: 300\n', encoding="utf-8")
    return path


@pytest.mark.parametrize("text,operation,valid", [
    ("/settings", "overview", True), ("/settings help", "help", True), ("/settings search 输出 上限", "search", True),
    ("/settings show MAX_TOKENS", "show", True), ("/settings show ../x", "show", False),
    ("/settings set max_tokens 32768", "set", True), ("/settings set max_tokens", "set", False),
    ("/settings set agent_name 我的 助手", "set", True), ("/settings reset max_tokens", "reset", True),
    ("/settings history", "history", True), ("/settings history bad-key", "history", False),
    ("/settings revert a1b2c3", "revert", True), ("/settings revert a1b", "revert", False),
    ("/settings delete max_tokens", "unknown", False),
])
def test_parser_validates_keys_ids_and_keeps_set_values(text, operation, valid):
    command = _settings(text)
    assert (command.operation, command.valid) == (operation, valid)
    assert "/settings set" in command.usage
    if text == "/settings set agent_name 我的 助手":
        assert command.value == "set agent_name 我的 助手"


def test_catalog_routes_through_gateway_and_tui_round_trips_the_text():
    assert match_conversation_command("/settings set max_tokens 32768") is not None
    command = _settings("/settings SET Max_Tokens 32768")
    text = control_runtime._command_text(command)
    assert text == "/settings set max_tokens 32768" and _settings(text) == command


def test_local_tui_mode_refuses_instead_of_falling_into_stop():
    execution = SimpleNamespace(state=SimpleNamespace(request_id="", running=True))
    result = control_runtime._execute_local_control(execution, _settings("/settings"))
    assert result.kind == "settings" and result.ok is False and "Gateway" in result.message


def test_gateway_dispatch_reaches_the_settings_service(monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service

    seen = []
    monkeypatch.setattr(module, "execute_settings_control",
                        lambda agent, command, scope: seen.append(command.operation) or "handled")
    assert control_service.execute_gateway_conversation_control(None, None, _settings("/settings"), None) == "handled"
    assert seen == ["overview"]


def test_only_admins_can_see_or_change_parameters(monkeypatch, user_config):
    refused = _run(monkeypatch, user_config, "/settings show max_tokens", home=_FEISHU_USER)
    assert refused.ok is False and "管理员" in refused.message and "65536" not in refused.message
    blank = SimpleNamespace(owner_provider="", owner_kind="", owner_id="")
    assert _run(monkeypatch, user_config, "/settings", home=blank).ok is False


def test_admin_can_find_change_review_and_revert(monkeypatch, user_config):
    overview = _run(monkeypatch, user_config, "/settings")
    assert overview.ok and "request_timeout = 300" in overview.message and "/restart" in overview.message
    found = _run(monkeypatch, user_config, "/settings search max_tokens")
    assert found.ok and found.message.splitlines()[1].startswith("- max_tokens［可改］当前 65536")
    shown = _run(monkeypatch, user_config, "/settings show max_tokens")
    assert "64K" in shown.message and "未覆盖" in shown.message and "可以修改" in shown.message
    changed = _run(monkeypatch, user_config, "/settings set max_tokens 32768")
    assert changed.ok and "已把 max_tokens 改为 32768" in changed.message and "/restart" in changed.message
    assert load_config(user_config).max_tokens == 32768
    history = _run(monkeypatch, user_config, "/settings history")
    change_id = history.message.splitlines()[1].split()[1]
    assert "max_tokens set" in history.message and "（chat）" in history.message
    reverted = _run(monkeypatch, user_config, f"/settings revert {change_id}")
    assert reverted.ok and "max_tokens:" not in user_config.read_text(encoding="utf-8")


def test_show_reports_the_applied_output_cap_for_the_default_model(monkeypatch, user_config):
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    agent = SimpleNamespace(config=SimpleNamespace(
        config_path=str(user_config), max_tokens=65536, model_context_window_tokens=131072, request_timeout=300))
    shown = module.execute_settings_control(agent, _settings("/settings show max_tokens"), None)
    assert shown.ok and "当前运行值：65536" in shown.message and "实际使用值：32768" in shown.message
    assert "/model" in shown.message
    plain = module.execute_settings_control(agent, _settings("/settings show request_timeout"), None)
    assert plain.ok and "实际使用值" not in plain.message


def test_boundary_and_unexpected_failures_are_reported_honestly(monkeypatch, user_config):
    boundary = _run(monkeypatch, user_config, "/settings set api_base http://evil.example")
    assert boundary.ok is False and "安全边界" in boundary.message
    shown = _run(monkeypatch, user_config, "/settings show system_prompt")
    assert "不能在这里修改" in shown.message

    def _broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "set_parameter", _broken)
    failed = _run(monkeypatch, user_config, "/settings set max_tokens 1024")
    assert failed.ok is False and "没能完整确认" in failed.message and "没有改动" not in failed.message
