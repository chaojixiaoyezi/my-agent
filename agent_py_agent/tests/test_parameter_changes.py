"""参数中心写入口（2026-09-27）：修改、恢复默认、修改记录与回滚，以及 my-agent 的 user_config 工具入口。

锁定：值按字段类型写入（布尔不加引号，避免 "false" 被当成真）；写后用正式 load_config 回读，不一致恢复原文件；
边界项、列表类型、随包默认 YAML 拒绝；每次写入记账，凭据只记脱敏值且不可回滚；回滚按编号前缀、可连续回滚；
工具路径取当前进程实际加载的配置文件（原来只看从未设置的 MY_AGENT_CONFIG，模型因此误报“没有用户级配置”）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin
from agent_py_agent.agent.settings.user_config_capability import (
    packaged_config_path,
    user_config_path,
)
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

_ORIGIN = ChangeOrigin("test")

def _user(tmp_path, text: str = "agent_name: \"myagent\"\n"):
    path = tmp_path / "desktop.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _set(path, key, value):
    return changes.set_parameter(key, value, user_path=path, origin=_ORIGIN)


def test_values_are_written_by_type_and_really_take_effect(tmp_path):
    path = _user(tmp_path)
    assert _set(path, "max_tokens", "32768")["ok"]
    report = _set(path, "enable_self_learning", "关闭")
    assert report["ok"] and report["effect_when"] == "restart_gateway" and "/restart" in report["effect_text"]
    assert _set(path, "agent_name", "小助手")["ok"]
    text = path.read_text(encoding="utf-8")
    assert "max_tokens: 32768" in text and "enable_self_learning: false" in text and 'agent_name: "小助手"' in text
    config = load_config(path)
    assert (config.max_tokens, config.enable_self_learning, config.agent_name) == (32768, False, "小助手")


def test_refusals_leave_the_file_untouched(tmp_path):
    path = _user(tmp_path)
    before = path.read_text(encoding="utf-8")
    cases = {("api_base", "http://evil.example/v1"): "PARAMETER_BOUNDARY", ("system_prompt", "x"): "PARAMETER_BOUNDARY",
             ("max_tokens", "abc"): "PARAMETER_INVALID", ("max_tokens", "-5"): "PARAMETER_INVALID",
             ("agent_name", 'a"b'): "PARAMETER_INVALID", ("model_input_modalities", "text"): "PARAMETER_STRUCTURED",
             ("no_such_key", "1"): "PARAMETER_UNKNOWN"}
    for (key, value), code in cases.items():
        report = _set(path, key, value)
        assert (report["ok"], report["code"]) == (False, code), key
    assert path.read_text(encoding="utf-8") == before
    assert changes.parameter_history(user_path=path) == []
    packaged = changes.set_parameter("max_tokens", "1024", user_path=packaged_config_path(), origin=_ORIGIN)
    assert packaged["code"] == "USER_CONFIG_IS_PACKAGED"


def test_a_value_the_loader_would_change_is_rolled_back(tmp_path, monkeypatch):
    path = _user(tmp_path)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(changes, "_effective", lambda _path, _key: 1)
    report = _set(path, "max_tokens", "32768")
    assert (report["ok"], report["code"]) == (False, "PARAMETER_NOT_EFFECTIVE")
    assert path.read_text(encoding="utf-8") == before


def test_reset_history_and_revert_chain(tmp_path):
    path = _user(tmp_path)
    first = _set(path, "max_tokens", "32768")
    second = _set(path, "max_tokens", "16384")
    assert [item["id"] for item in changes.parameter_history(user_path=path)] == [second["change_id"], first["change_id"]]
    undo = changes.revert_change(second["change_id"][:6], user_path=path, origin=_ORIGIN)
    assert undo["ok"] and load_config(path).max_tokens == 32768
    back_to_default = changes.revert_change(first["change_id"], user_path=path, origin=_ORIGIN)
    assert back_to_default["ok"] and "max_tokens:" not in path.read_text(encoding="utf-8")
    assert changes.reset_parameter("max_tokens", user_path=path, origin=_ORIGIN)["code"] == "PARAMETER_NOT_OVERRIDDEN"
    _set(path, "request_timeout", "300")
    reset = changes.reset_parameter("request_timeout", user_path=path, origin=_ORIGIN)
    assert reset["ok"] and load_config(path).request_timeout == AgentConfig().request_timeout
    assert changes.revert_change("abc", user_path=path, origin=_ORIGIN)["code"] == "CHANGE_NOT_FOUND"


def test_secret_changes_are_masked_and_cannot_be_reverted(tmp_path):
    path = _user(tmp_path)
    report = _set(path, "feishu_app_secret", "abcdefsecret")
    assert report["ok"] and "abcdefsecret" not in json.dumps(report, ensure_ascii=False)
    entry = changes.parameter_history(user_path=path)[0]
    assert entry["masked"] and "abcdefsecret" not in json.dumps(entry, ensure_ascii=False)
    assert changes.revert_change(entry["id"], user_path=path, origin=_ORIGIN)["code"] == "CHANGE_MASKED"


def _main_agent(path):
    home = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
    return SimpleNamespace(home_paths=home, config=SimpleNamespace(config_path=str(path), max_tokens=65536))


def test_tool_uses_the_loaded_config_file_and_exposes_the_parameter_center(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = _user(tmp_path)
    agent = _main_agent(path)
    assert user_config_path(agent.config) == path and user_config_path(None) is None
    tool = UserConfigTool(agent)
    view = json.loads(tool.execute({"action": "view", "key": "max_tokens"}).output)
    assert view["user_config_path"] == str(path) and view["parameter"]["writable"] and view["parameter"]["running_value"] == "65536"
    assert "tunable" not in view["fact"]  # 能否修改只看登记表的 writable（旧白名单字段曾让模型误以为改不了）
    assert view["parameter"]["applied_value"] == 65536  # 替身配置没有窗口，实际使用值等于配置值
    agent.config.model_context_window_tokens = 131072
    narrowed = json.loads(tool.execute({"action": "view", "key": "max_tokens"}).output)["parameter"]
    assert narrowed["running_value"] == "65536" and narrowed["applied_value"] == 32768
    del agent.config.model_context_window_tokens
    found = json.loads(tool.execute({"action": "search", "query": "max_tokens"}).output)["parameters"]
    assert found[0]["key"] == "max_tokens"
    saved = tool.execute({"action": "set", "key": "max_tokens", "value": "32768", "reason": "用户要求"})
    assert saved.ok and json.loads(saved.output)["saved_to"] == str(path)
    history = json.loads(tool.execute({"action": "history"}).output)["changes"]
    assert history[0]["actor"] == "model" and history[0]["reason"] == "用户要求"
    denied = tool.execute({"action": "set", "key": "api_base", "value": "http://x"})
    assert not denied.ok and denied.error_code == "TOOL_PERMISSION_DENIED"
    reverted = tool.execute({"action": "revert", "change_id": history[0]["id"]})
    assert reverted.ok and "max_tokens:" not in path.read_text(encoding="utf-8")
