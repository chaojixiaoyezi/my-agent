"""各回显出口的配置值文字一致（参数中心，2026-09-27）：mask_value 是唯一口径。

背景：mask_value 原为 `str(x or "")`，user_config 的运行值、查看报告、改参回执和 config get 把 False、0 都给成空串，
/settings 另写了一份布尔、数字特判。现在所有出口共用 mask_value：非凭据的布尔显示 true/false，数字照实显示，
None、空串、空列表、空映射显示空串，凭据遮住；聊天 /settings 把空串显示成“（空）”，这是唯一的界面差异。
每一格都走出口的真实代码路径（真实用户配置文件 + load_config）；走不到的格子由测试核对它确实走不到，不许悄悄扩大。
"""
from __future__ import annotations

import json
import re
from argparse import Namespace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service as settings_module
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.cli.config_cmd import cmd_config_get

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
_EMPTY = "（空）"
_EXITS = ("view_fact", "view_running", "search_running", "receipt", "settings_running", "settings_user", "config_get")
# 每行：名称、键、写进用户配置的 YAML 文本（None=不写这个键）、聊天改参动作、期望文字。
# 改参动作：("set", 值) 直接改；("reset", 先写的值) 先改再恢复默认；None 表示聊天里改不了。
_ROWS = [
    ("false", "enable_self_learning", "false", ("set", "false"), "false"),
    ("true", "enable_self_learning", "true", ("set", "true"), "true"),
    ("zero", "max_parallel_tool_calls", "0", ("set", "0"), "0"),
    ("none", "temperature", None, ("reset", "0.7"), ""),
    ("empty_text", "timezone", '""', ("set", ""), ""),
    ("empty_list", "tool_catalog_categories", "[]", None, ""),
    ("empty_map", "runner_timeout_by_role", "{}", None, ""),
    ("credential", "api_key", '"sk-FAKE-9999"', None, "sk-***"),
]
# 走不到的格子：简易 YAML 写不出 None，没写的键 config get 打印“(未设置)”、/settings 显示“未覆盖”；
# 列表、映射不在聊天里改；凭据是安全边界，不能写。
_NOT_APPLICABLE = {
    ("none", "config_get"), ("none", "settings_user"),
    ("empty_list", "receipt"), ("empty_map", "receipt"), ("credential", "receipt"),
}


def _settings_show(monkeypatch, config, key: str) -> str:
    monkeypatch.setattr(settings_module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    command = parse_conversation_control(f"/settings show {key}", reject_unknown_slash=True)
    result = settings_module.execute_settings_control(SimpleNamespace(config=config), command, None)
    assert result.ok, result.message
    return result.message


# 改参回执用单独的配置文件，不影响其它出口读到的值；改不了的返回 None。
def _receipt_text(tmp_path, key: str, change):
    path = tmp_path / "receipt.yaml"
    path.write_text('agent_name: "myagent"\n', encoding="utf-8")
    tool = UserConfigTool(SimpleNamespace(home_paths=_ADMIN, config=load_config(path)))
    if change is None:
        assert not tool.execute({"action": "set", "key": key, "value": "x"}).ok
        return None
    action, value = change
    outcome = tool.execute({"action": "set", "key": key, "value": value})
    if action == "reset":
        outcome = tool.execute({"action": "reset", "key": key})
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)["effective"]


def _exit_texts(tmp_path, monkeypatch, capsys, row) -> dict[str, object]:
    _name, key, yaml_text, change, _expected = row
    path = tmp_path / "desktop.yaml"
    extra = f"{key}: {yaml_text}\n" if yaml_text is not None else ""
    path.write_text(f'agent_name: "myagent"\n{extra}', encoding="utf-8")
    config = load_config(path)
    tool = UserConfigTool(SimpleNamespace(home_paths=_ADMIN, config=config))
    view = json.loads(tool.execute({"action": "view", "key": key}).output)
    found = json.loads(tool.execute({"action": "search", "query": key}).output)["parameters"]
    shown = _settings_show(monkeypatch, config, key)
    capsys.readouterr()
    cmd_config_get(Namespace(config=str(path), key=key))
    return {
        "view_fact": view["fact"]["effective"],
        "view_running": view["parameter"]["running_value"],
        "search_running": next(item["running_value"] for item in found if item["key"] == key),
        "receipt": _receipt_text(tmp_path, key, change),
        "settings_running": re.search(r"当前运行值：(.*?)；用户配置里", shown).group(1),
        "settings_user": re.search(r"用户配置里：(.*)", shown).group(1),
        "config_get": capsys.readouterr().out.rstrip("\n").removeprefix(f"{key}: "),
    }


@pytest.mark.parametrize("row", _ROWS, ids=[row[0] for row in _ROWS])
def test_every_exit_shows_the_same_text_for_the_same_value(row, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)  # 环境变量里的 key 会盖掉配置文件里的假凭据
    name, key, _yaml_text, _change, expected = row
    texts = _exit_texts(tmp_path, monkeypatch, capsys, row)

    unreachable = {exit_name: texts[exit_name] for exit_name in _EXITS if (name, exit_name) in _NOT_APPLICABLE}
    for exit_name, text in unreachable.items():
        assert text is None or text == f"(未设置) {key}" or str(text).startswith("未覆盖"), (exit_name, text)
    shown = {exit_name: texts[exit_name] for exit_name in _EXITS if exit_name not in unreachable}
    wanted = {exit_name: (expected or _EMPTY) if exit_name.startswith("settings_") else expected for exit_name in shown}
    assert shown == wanted
