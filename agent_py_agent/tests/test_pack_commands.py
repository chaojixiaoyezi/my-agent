"""learnpack 第 4 步：能力包命令 /plugins#（TUI、IM 同一入口）。

锁定：/plugins# 被所有入口认成系统命令（命令名解析、插件命名空间、会话控制解析），/plugins@ 不受影响；帮助与 TUI 斜杠菜单、
补全里能看到 /plugins#；列表标出"她做的"（按装着的字节摘要在 learnpack 存储里有记录判断）并写来源与许可证；查看给详情和
/plugins# 写法的管理命令；启用/停用/删除转成同一条管理员 /plugins 命令；退回只退到她做过的上一版；"安装 <单号>"执行同名包的
确认单、包名对不上被拒；每个动作 TUI、IM 各走一遍真实链路；普通用户能看列表与详情但不能改；未知包、未知动作、多余参数给登记过的
错误码；安装回执里能力包的确认行与管理命令都是 /plugins# 写法，插件仍是 /plugins 写法；/plugins list 也标出她做的插件。
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.learnpack_store import LearnpackStore
from agent_py_agent.agent.command_catalog import COMMAND_INDEX, system_slash_command_name
from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.plugin_commands import (
    parse_plugin_command,
    plugin_namespace,
    render_plugin_help,
)
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    owner_identity_from_config,
    resolve_owner_home,
)

ROOT = Path(__file__).resolve().parents[2]


def test_pack_namespace_is_a_system_command_everywhere_and_plugins_at_is_unchanged():
    assert system_slash_command_name("/plugins#drama-scenes 查看") == "plugins#drama-scenes"
    pack = plugin_namespace("/plugins#drama-scenes 查看")
    assert (pack.kind, pack.plugin_id, pack.prefix, pack.body) == ("pack", "drama-scenes", "/plugins#drama-scenes", "查看")
    listing = plugin_namespace("/plugins#")
    assert (listing.kind, listing.plugin_id, listing.prefix) == ("pack", "", "/plugins#")
    plugin = plugin_namespace("/plugins@hello hello")
    assert (plugin.kind, plugin.plugin_id, plugin.prefix) == ("plugin", "hello", "/plugins@hello")
    assert plugin_namespace("/plugins list").kind == "plugin"
    for text in ("/plugins#drama-scenes 停用", "/plugins#"):
        command = parse_conversation_control(text, reject_unknown_slash=True)
        assert command is not None and command.kind == "plugins" and command.valid, text


def test_help_and_slash_menu_show_the_pack_entry():
    variants = " ".join(text for text, _summary in COMMAND_INDEX["plugins"].help_variants)
    assert "/plugins#" in variants and COMMAND_INDEX["plugins"].namespace_separators == ("@", "#")
    assert "/plugins#" in render_plugin_help(plugin_namespace("/plugins"), COMMAND_INDEX["plugins"].actions)
    with pytest.raises(Exception) as caught:  # 能力包入口不进插件动作解析
        parse_plugin_command("/plugins#drama-scenes 查看")
    assert getattr(caught.value, "reason", "") == "pack_namespace"


def test_tui_completion_offers_pack_action_words():
    from agent_py_agent.agent.plugin_completion import complete_plugin_command

    items = complete_plugin_command("/plugins#drama-scenes ")
    assert [item.label for item in items] == ["查看", "启用", "停用", "删除", "退回", "安装"]
    assert items[0].text == "查看", "中文动作词原样写回，不加引号"
    assert [item.label for item in complete_plugin_command("/plugins#drama-scenes 退")] == ["退回"]
    assert complete_plugin_command("/plugins#drama-scenes 停用 ") == ()


# ---------- 真实链路：SimpleAgent + 真实 /plugins 命令链 ----------
def _agent(tmp_path, monkeypatch) -> SimpleAgent:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider="local",
                                   my_agent_owner_kind="main", my_agent_owner_id="main"), tmp_path / "project")


def _manager(agent, *, is_admin=True) -> PluginManagement:
    home = agent.home_paths
    owner = resolve_owner_home(home.root, owner_identity_from_config(agent.config))
    return PluginManagement(plugin_management_context(owner, home, agent.config, agent.conversation_store.threads,
                                                      actor_id="local-agent", channel="chat",
                                                      conversation_id="tui-1", is_admin=is_admin))


# 和真 TUI 一样：每条命令带当前目录版本和一个新请求编号。
def _tui(agent, text, *, is_admin=True) -> dict:
    manager = _manager(agent, is_admin=is_admin)
    return manager.command(text, revision=manager.catalog().revision, request_id=f"tui-{uuid.uuid4().hex}")


def _im(agent, text):
    host = SimpleNamespace(config=agent.config, home_paths=agent.home_paths)
    scope = GatewayControlScope(user_id="alice", channel="feishu", conversation_id="dm-1",
                                metadata={"message_id": f"im-{abs(hash(text)) % 10**8}"},
                                resolved_owner=OwnerIdentity("local", "main", "main"))
    command = parse_conversation_control(text, reject_unknown_slash=True)
    result = control_service.execute_gateway_conversation_control(host, None, command, scope)
    return {"ok": result.ok, "message": result.message, "error_code": result.error_code}


def _entry(agent, package_id):
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    return next((row for row in PluginInstallStore(owner).snapshot() if row.manifest.plugin_id == package_id), None)


def _order(agent, version="0.1.0") -> dict:
    root = Path(agent.home_paths.owner_home_dir) / "runs" / "2026-10-06" / "learn" / f"drama-{version}"
    root.mkdir(parents=True)
    (root / "CAPABILITY.md").write_text(f"# 短剧分场 {version}\n", encoding="utf-8")
    built = agent.tools.tools["package_build"].execute({
        "kind": "capability_pack", "source_dir": str(root), "origin": "github.com/example/drama-agent@abc", "license": "MIT",
        "declaration": {"plugin_id": "drama-scenes", "version": version, "summary": "短剧分场方法",
                        "capability": {"description": "把短剧故事拆成场次", "keywords": ["短剧"], "entry_document": "CAPABILITY.md"}}})
    sha = json.loads(built.output)["build"]["sha256"]
    return json.loads(agent.tools.tools["package_install"].execute({"sha256": sha}).output)


@pytest.mark.parametrize("entry", [_tui, _im])
def test_every_pack_action_works_in_tui_and_im(tmp_path, monkeypatch, entry):
    agent = _agent(tmp_path, monkeypatch)
    first = _order(agent, "0.1.0")
    assert first["user_confirm_command"] == f"/plugins#drama-scenes 安装 {first['order_id']}"
    assert entry(agent, first["user_confirm_command"])["ok"] and _entry(agent, "drama-scenes").enabled
    listing = entry(agent, "/plugins#")
    assert listing["ok"] and "- drama-scenes 0.1.0（启用，她做的）\n" in listing["message"], listing
    assert "\n  她声明的来源与许可证（原文，宿主未核实）：github.com/example/drama-agent@abc；MIT" in listing["message"]
    view = entry(agent, "/plugins#drama-scenes 查看")
    assert view["ok"] and "她做的（打包于" in view["message"] and "/plugins#drama-scenes 退回" in view["message"]
    assert "带检查程序：否" in view["message"]
    assert entry(agent, "/plugins#drama-scenes 停用")["ok"] and not _entry(agent, "drama-scenes").enabled
    assert entry(agent, "/plugins#drama-scenes 启用")["ok"] and _entry(agent, "drama-scenes").enabled
    second = _order(agent, "0.2.0")
    assert entry(agent, second["user_confirm_command"])["ok"] and _entry(agent, "drama-scenes").manifest.version == "0.2.0"
    preview = entry(agent, "/plugins#drama-scenes 退回")
    line = preview["message"].splitlines()[-1]
    assert preview["ok"] and line.startswith("/plugins#drama-scenes 退回 "), preview
    assert _entry(agent, "drama-scenes").manifest.version == "0.2.0", "预览什么都不做"
    reverted = entry(agent, line)
    assert reverted["ok"] and _entry(agent, "drama-scenes").manifest.version == "0.1.0", reverted
    assert entry(agent, "/plugins#drama-scenes 停用")["ok"]
    assert entry(agent, "/plugins#drama-scenes 删除")["ok"] and _entry(agent, "drama-scenes") is None


def test_pack_install_rejects_an_order_for_another_package(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    body = _order(agent)
    reply = _tui(agent, f"/plugins#other-pack 安装 {body['order_id']}")
    assert not reply["ok"] and reply["error_code"] == "PACKAGE_INSTALL_ORDER_NOT_FOUND"
    assert _entry(agent, "drama-scenes") is None and _tui(agent, f"/plugins confirm {body['order_id']}")["ok"]


def test_non_admin_can_read_but_not_change(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    body = _order(agent)
    _tui(agent, body["user_confirm_command"])
    assert _tui(agent, "/plugins#", is_admin=False)["ok"] and _tui(agent, "/plugins#drama-scenes", is_admin=False)["ok"]
    refused = _tui(agent, "/plugins#drama-scenes 停用", is_admin=False)
    assert not refused["ok"] and refused["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert _entry(agent, "drama-scenes").enabled


def test_unknown_pack_action_and_extra_arguments_are_coded(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    empty = _tui(agent, "/plugins#")
    assert empty["ok"] and "当前没有装能力包" in empty["message"]
    missing = _tui(agent, "/plugins#nope 查看")
    assert not missing["ok"] and missing["error_code"] == "PACK_NOT_FOUND" and "PACK_NOT_FOUND" in ERROR_CONTRACTS
    for text in ("/plugins#nope 飞起来", "/plugins#nope 停用 多余", "/plugins#nope 安装", "/plugins#nope 安装 x", "/plugins# 删除"):
        reply = _tui(agent, text)
        assert not reply["ok"] and reply["error_code"] == "INVALID_COMMAND_ARGUMENTS", text


def test_plugins_keep_the_plain_management_lines_and_are_marked_in_the_list(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    plugin_root = Path(agent.home_paths.owner_home_dir) / "runs" / "2026-10-06" / "learn" / "hello-node"
    shutil.copytree(ROOT / "plugins" / "hello-node", plugin_root)
    declaration = json.loads((plugin_root / "declaration.json").read_text(encoding="utf-8"))
    built = agent.tools.tools["package_build"].execute({"kind": "plugin", "source_dir": str(plugin_root),
                                                        "declaration": declaration, "origin": "自己写的", "license": "自有"})
    body = json.loads(agent.tools.tools["package_install"].execute(
        {"sha256": json.loads(built.output)["build"]["sha256"]}).output)
    assert body["user_confirm_command"] == f"/plugins confirm {body['order_id']}"
    wrong = _tui(agent, f"/plugins#hello-node 安装 {body['order_id']}")
    assert not wrong["ok"] and wrong["error_code"] == "PACKAGE_INSTALL_ORDER_NOT_FOUND" and "/plugins confirm" in wrong["message"]
    assert LearnpackStore(agent.home_paths.owner_home_dir).order_outcome(body["order_id"]) is None, "插件的单不能用能力包写法确认"
    _tui(agent, body["user_confirm_command"])
    listing = _tui(agent, "/plugins list")
    assert "hello-node" in listing["message"] and "my-agent 自己做的：hello-node" in listing["message"]
    note = next(line for line in listing["message"].splitlines() if line.startswith("my-agent 自己做的"))
    assert "/plugins#" not in note, "不给插件指只管能力包的入口"
    assert "hello-node" not in _tui(agent, "/plugins#")["message"], "插件不进能力包列表"
    plugin_as_pack = _tui(agent, "/plugins#hello-node 查看")
    assert not plugin_as_pack["ok"] and plugin_as_pack["error_code"] == "PACK_NOT_FOUND", "插件不当能力包管理"
    assert LearnpackStore(agent.home_paths.owner_home_dir).build(json.loads(built.output)["build"]["sha256"]) is not None


def test_a_retried_pack_command_replays_the_original_request(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    body = _order(agent)
    assert _tui(agent, body["user_confirm_command"])["ok"]
    revision = _manager(agent).catalog().revision
    first = _manager(agent).command("/plugins#drama-scenes 停用", revision=revision, request_id="retry-1")
    again = _manager(agent).command("/plugins#drama-scenes 停用", revision=revision, request_id="retry-1")
    assert first["ok"] and again["ok"] and first["request_id"] == again["request_id"] == "retry-1", (first, again)
    assert "/plugins status retry-1" in again["message"] and not _entry(agent, "drama-scenes").enabled


def test_pack_actions_reply_exactly_like_the_plugin_command(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    _tui(agent, _order(agent)["user_confirm_command"])
    assert _tui(agent, "/plugins#drama-scenes 停用")["ok"]
    via_pack = _tui(agent, "/plugins#drama-scenes 启用")
    assert _tui(agent, "/plugins disable drama-scenes")["ok"]
    direct = _tui(agent, "/plugins enable drama-scenes")
    strip = lambda reply: reply["message"].replace(reply["request_id"], "<编号>")  # noqa: E731
    assert via_pack["ok"] and strip(via_pack) == strip(direct), "转发的回执由宿主原样给出，不再包一层"


def test_gateway_logs_pack_commands_under_their_own_action_name():
    from agent_py_agent.agent.gateway_parts.plugin_command_service import _action_name

    assert _action_name("/plugins#drama-scenes 停用") == "pack" and _action_name("/plugins#") == "pack"
    assert _action_name("/plugins@hello hello") == "plugin_call" and _action_name("/plugins list") == "list"


def test_view_says_whether_the_pack_runs_checkers(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    root = Path(agent.home_paths.owner_home_dir) / "runs" / "2026-10-06" / "learn" / "drama-text-a"
    shutil.copytree(ROOT / "examples" / "capability-packages" / "drama-text-a", root)
    declaration = json.loads((root / "declaration.json").read_text(encoding="utf-8"))
    built = agent.tools.tools["package_build"].execute({"kind": "capability_pack", "source_dir": str(root),
                                                        "declaration": declaration, "origin": "仓库样例", "license": "自有"})
    body = json.loads(agent.tools.tools["package_install"].execute({"sha256": json.loads(built.output)["build"]["sha256"]}).output)
    assert _tui(agent, body["user_confirm_command"])["ok"]
    view = _tui(agent, "/plugins#drama-text-a 查看")
    assert view["ok"] and "带检查程序：是" in view["message"], view
