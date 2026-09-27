"""字典与列表参数的结构脱敏（2026-09-27）：原来只按顶层键名判断再把整个值 str()，请求头、MCP 的 env/args 会整段明文回显。

锁定：请求头与环境变量映射只留键名；嵌套的凭据键、命令行凭据开关、名字是凭据的“名字=值”、网址里的密码与凭据查询参数都遮值，
--header/--env 的值只留名字；普通字典与列表照常显示；
每个回显与记账出口（user_config view/search/history、/settings 总览/查看/搜索/历史、修改回执与修改记录、命令行 config-get）
都看不到明文，脱敏过的记录不能回滚。全部用假值，不读任何真实配置。
"""
from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service as settings
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin, ledger_path
from agent_py_agent.agent.settings.user_config_capability import mask_value, masked_structure
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.cli import config_cmd

_SECRETS = ("FAKE-HEADER-1", "FAKE-ENV-2", "FAKE-ARG-3", "FAKE-FLAG-4", "FAKE-PW-5", "FAKE-QUERY-6", "FAKE-NESTED-7",
            "FAKE-HDRARG-8", "FAKE-DOCKER-9", "FAKE-CBPW-10")
_HEADERS = {"Authorization": "Bearer FAKE-HEADER-1", "X-Custom-Signature": "FAKE-HEADER-1"}
# args 覆盖常见启动写法：凭据开关、--开关=值、网址、mcp-remote 的 --header、docker 的 -e 名字=值、--开关=带密码网址
_SERVERS = {"github": {
    "command": "npx", "cwd": "/srv/gh", "env": {"GITHUB_TOKEN": "FAKE-ENV-2", "LOG_LEVEL": "debug"},
    "args": ["-y", "gh-server", "--api-key", "FAKE-ARG-3", "--token=FAKE-FLAG-4",
             "https://bot:FAKE-PW-5@hooks.example/x?token=FAKE-QUERY-6&page=2",
             "--header", "Authorization: Bearer FAKE-HDRARG-8", "-e", "GITHUB_TOKEN=FAKE-DOCKER-9",
             "-e", "LOG_LEVEL=debug", "--env", "PASS_THROUGH", "--callback=https://bot:FAKE-CBPW-10@hooks.example/cb"],
    "auth": {"client_secret": "FAKE-NESTED-7", "client_id": "public-id"},
}}
_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
_URL_WITH_SECRETS = "https://bot:FAKE-PW-5@hooks.example/x?token=FAKE-QUERY-6"


# 函数用途: 列出一段输出里出现的假凭据（应当为空）。
def _leaks(text: str) -> list[str]:
    return [secret for secret in _SECRETS if secret in text]


# 函数用途: 写一份带请求头、MCP 服务器与普通字典的临时用户配置。
def _user_config(tmp_path):
    path = tmp_path / "desktop.yaml"
    path.write_text("model_custom_headers: " + json.dumps(_HEADERS) + "\nmcp_servers: " + json.dumps(_SERVERS)
                    + '\nrunner_timeout_by_role: {"coder": 600}\nagent_name: "myagent"\n', encoding="utf-8")
    return path


# 函数用途: 与用户配置同内容的运行中配置替身。
def _running(path) -> SimpleNamespace:
    return SimpleNamespace(config_path=str(path), model_custom_headers=_HEADERS, mcp_servers=_SERVERS,
                           runner_timeout_by_role={"coder": 600})


# 函数用途: 以管理员身份执行一条聊天 /settings。
def _settings(monkeypatch, config, text: str):
    monkeypatch.setattr(settings, "_scoped_home", lambda _agent, _scope: _ADMIN)
    command = parse_conversation_control(text, reject_unknown_slash=True)
    return settings.execute_settings_control(SimpleNamespace(config=config), command, None)


def test_masking_keeps_the_structure_and_hides_every_secret_position():
    masked = masked_structure("mcp_servers", _SERVERS)["github"]
    assert masked["command"] == "npx" and masked["cwd"] == "/srv/gh" and masked["auth"]["client_id"] == "public-id"
    assert set(masked["env"]) == {"GITHUB_TOKEN", "LOG_LEVEL"} and masked["args"][:3] == ["-y", "gh-server", "--api-key"]
    assert masked["args"][4].startswith("--token=") and masked["args"][5].endswith("&page=2")
    # 请求头只留名字，凭据名的环境变量遮值，普通变量与纯变量名照常显示，--开关=网址 里的密码遮住
    assert masked["args"][6:] == ["--header", "Authorization: Be***", "-e", "GITHUB_TOKEN=FAK***", "-e", "LOG_LEVEL=debug",
                                  "--env", "PASS_THROUGH", "--callback=https://bot:***@hooks.example/cb"]
    assert masked_structure("args", ["--no-token", "--verbose"]) == ["--no-token", "--verbose"]  # 下一项是开关，不当值遮
    headers = masked_structure("model_custom_headers", _HEADERS)
    assert set(headers) == set(_HEADERS)
    assert not _leaks(str(masked) + str(headers))
    assert _SERVERS["github"]["env"]["GITHUB_TOKEN"] == "FAKE-ENV-2"  # 返回副本，不改原值
    # 普通字典、列表与标量照常显示；顶层凭据仍按原口径遮住
    assert mask_value("runner_timeout_by_role", {"coder": 600}) == "{'coder': 600}"
    assert mask_value("path_dangerous_roots", ["/etc", "/bin"]) == "['/etc', '/bin']"
    assert masked_structure("tool_catalog_deferred_categories", ("web", "mcp")) == ("web", "mcp")
    assert mask_value("api_key", "sk-FAKE-9999") == "sk-***" and mask_value("max_tokens", 0) == ""
    assert mask_value("agent_name", '"http://u:FAKE-PW-5@proxy:8080"') == '"http://u:***@proxy:8080"'
    assert mask_value("agent_name", "plain://text") == "plain://text"


def test_user_config_view_and_search_never_show_structured_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = _user_config(tmp_path)
    tool = UserConfigTool(SimpleNamespace(home_paths=_ADMIN, config=_running(path)))
    for params in ({"action": "view", "key": "model_custom_headers"}, {"action": "view", "key": "mcp_servers"},
                   {"action": "search", "query": "mcp"}, {"action": "search", "query": "headers"}):
        outcome = tool.execute(params)
        assert outcome.ok and not _leaks(outcome.output), params
    ordinary = json.loads(tool.execute({"action": "view", "key": "runner_timeout_by_role"}).output)
    assert ordinary["parameter"]["running_value"] == "{'coder': 600}"


def test_settings_overview_show_and_search_never_show_structured_secrets(tmp_path, monkeypatch):
    config = _running(_user_config(tmp_path))
    for text in ("/settings", "/settings show model_custom_headers", "/settings show mcp_servers",
                 "/settings search mcp", "/settings search headers"):
        result = _settings(monkeypatch, config, text)
        assert result.ok and not _leaks(result.message), text
    assert "{'coder': 600}" in _settings(monkeypatch, config, "/settings show runner_timeout_by_role").message


def test_receipts_ledger_and_history_are_masked_and_masked_rows_cannot_be_reverted(tmp_path, monkeypatch):
    path = _user_config(tmp_path)
    report = changes.set_parameter("agent_name", _URL_WITH_SECRETS, user_path=path, origin=ChangeOrigin("test"))
    assert report["ok"] and not _leaks(json.dumps(report, ensure_ascii=False))
    ledger = ledger_path(path).read_text(encoding="utf-8")
    assert not _leaks(ledger) and json.loads(ledger.splitlines()[-1])["masked"] is True
    refused = changes.revert_change(report["change_id"], user_path=path, origin=ChangeOrigin("test"))
    assert refused["ok"] is False and refused["code"] == "CHANGE_MASKED"  # 回滚会把 *** 写回配置，拒绝
    # 脱敏收紧前写下的旧记录可能含明文：历史回显出口仍然遮住
    old_row = {"id": "0ld0ld0ld0ld", "at": "2026-09-01T00:00:00+00:00", "key": "agent_name", "action": "set",
               "actor": "model", "reason": "", "masked": False, "previous": None, "value": f'"{_URL_WITH_SECRETS}"'}
    with ledger_path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old_row, ensure_ascii=False) + "\n")
    tool = UserConfigTool(SimpleNamespace(home_paths=_ADMIN, config=_running(path)))
    history = tool.execute({"action": "history"})
    assert history.ok and "0ld0ld0ld0ld" in history.output and not _leaks(history.output)
    chat = _settings(monkeypatch, _running(path), "/settings history")
    assert chat.ok and "0ld0ld0l" in chat.message and not _leaks(chat.message)
    assert json.loads(ledger_path(path).read_text(encoding="utf-8").splitlines()[-1])["value"].count("FAKE") == 2  # 原记录不被改写


def test_cli_config_get_masks_structured_values(tmp_path, capsys):
    path = _user_config(tmp_path)
    for key in ("model_custom_headers", "mcp_servers"):
        assert config_cmd.cmd_config_get(Namespace(key=key, config=str(path))) == 0
        output = capsys.readouterr().out
        assert key in output and not _leaks(output)
