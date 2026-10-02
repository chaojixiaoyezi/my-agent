# LLM: J16 片 F：/plugins list 末尾的 MCP 服务段。渲染只读 mcp_server_facts 的结构化事实（服务名、运行、发布状态、工具数、
#   原因码、提醒），publication 为 None 的客户端不列；TUI 直连把本进程注册表交给管理服务，Gateway/IM 只借用已加载的 owner 实例，
#   冷 owner 不初始化、段里写“未加载”。改 render_mcp_server_section / PluginManagementContext.live_registry 时先跑这里。
# 模块用途: 钉住 MCP 段的文字投影和两条入口的注册表来源。
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from agent_py_agent.agent.gateway_parts import plugin_command_service, request_worker
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.plugin_commands import render_mcp_server_section
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.mcp_declarations import MCPPublication
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_plugin_management import manager


# 函数用途: 假客户端：只给 mcp_server_facts 要读的三样（config.name、is_running、publication）。
def client(name: str, running: bool, publication: MCPPublication | None):
    return SimpleNamespace(config=SimpleNamespace(name=name), is_running=lambda: running, publication=publication)


def registry(*clients):
    return SimpleNamespace(_mcp_clients=tuple(clients))


PUBLISHED = MCPPublication("published", tool_count=18)
REJECTED = MCPPublication("rejected", code="MCP_DECLARATION_INVALID", reasons=({"tool": "click", "code": "observation_ref_param"},))
UNAVAILABLE = MCPPublication("unavailable", code="MCP_CONNECTION_CLOSED")


def test_section_renders_each_publication_state_and_skips_plugin_clients():
    text = render_mcp_server_section(registry(
        client("computer_use", True, PUBLISHED), client("browser", False, REJECTED), client("notes", True, UNAVAILABLE),
        client("plugin_sample", True, None), client("late", True, MCPPublication("published", tool_count=2, notices=({"tool": "x", "code": "declared_tool_not_discovered"},))),
    ))
    assert text.splitlines() == [
        "MCP 服务（4 个）：",
        "computer_use  运行中  已发布 18 个工具",
        "browser  未运行  被拒绝（MCP_DECLARATION_INVALID：tool=click code=observation_ref_param）",
        "notes  运行中  不可用（MCP_CONNECTION_CLOSED）",
        "late  运行中  已发布 2 个工具，提醒 1 条",
    ]
    assert "plugin_sample" not in text, "插件客户端没有发布链事实，不进 MCP 段"


def test_section_without_instance_or_servers_says_so():
    assert render_mcp_server_section(None) == "MCP 服务：当前实例未加载，没有运行事实。"
    assert render_mcp_server_section(registry()) == "MCP 服务：没有配置。"
    assert render_mcp_server_section(registry(client("plugin_only", True, None))) == "MCP 服务：没有配置。"


def test_management_list_appends_the_section_from_the_live_registry(tmp_path):
    service, _source = manager(tmp_path, live_registry=registry(client("computer_use", True, PUBLISHED)))
    result = service.command("/plugins list", revision=service.catalog().revision, request_id="")
    assert result["ok"] and result["message"].endswith("MCP 服务（1 个）：\ncomputer_use  运行中  已发布 18 个工具")
    assert result["message"].startswith("当前没有符合条件的已安装插件。\n\n")
    (tmp_path / "cold").mkdir()
    cold, _source = manager(tmp_path / "cold")
    assert cold.command("/plugins list", revision=cold.catalog().revision, request_id="")["message"].endswith(
        "MCP 服务：当前实例未加载，没有运行事实。")


def test_direct_tui_client_hands_its_own_registry_to_the_manager(monkeypatch, tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    from agent_py_agent.cli.chat_parts import plugin_command_client
    from agent_py_agent.cli.chat_parts.plugin_command_client import PluginCommandClient

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", Mock(side_effect=AssertionError("本地不发 HTTP")))
    owner = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("local", "alice"))
    agent = SimpleNamespace(
        config=AgentConfig(my_agent_owner_provider="local", my_agent_owner_kind="user", my_agent_owner_id="alice", gateway_port=9876),
        home_paths=home_paths_with_owner(home_paths(tmp_path), owner),
        conversation_store=ConversationStore(owner.home_dir / "conversations", initialize=False),
        effective_workspace_root=owner.home_dir, tools=registry(client("computer_use", True, PUBLISHED)),
    )
    client_ = PluginCommandClient(agent, "session-a", use_gateway=False)
    assert client_.command("/plugins help")["ok"]
    result = client_.command("/plugins list", revision=client_.snapshot().revision)
    assert result["ok"] and result["message"].endswith("computer_use  运行中  已发布 18 个工具")


# 函数用途: Gateway 侧的 IM 命令入口：base 是 Gateway 基础代理的替身；owner 走 per-user 作用域。
def _gateway_base(tmp_path, **attrs):
    return SimpleNamespace(config=AgentConfig(gateway_per_user_owner_scoping=True), home_paths=home_paths(tmp_path), **attrs)


def _scope(user="alice"):
    return GatewayControlScope(user_id=user, channel="feishu", conversation_id="session-a",
                               resolved_owner=OwnerIdentity.provider_user("feishu", user))


def test_gateway_cold_owner_stays_cold_and_section_says_not_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(request_worker, "_owner_pool", Mock(side_effect=AssertionError("禁止初始化完整 Agent")))
    management = plugin_command_service._scope_management(_gateway_base(tmp_path), _scope(), False)
    assert management.context.live_registry is None
    result = management.command("/plugins list", revision=management.catalog().revision, request_id="")
    assert result["message"].endswith("MCP 服务：当前实例未加载，没有运行事实。")


def test_gateway_loaded_owner_registry_feeds_the_section(tmp_path, monkeypatch):
    monkeypatch.setattr(request_worker, "_owner_pool", Mock(side_effect=AssertionError("禁止初始化完整 Agent")))
    live = registry(client("computer_use", True, PUBLISHED))
    peeked = []

    class Pool:
        def peek(self, owner):
            peeked.append((owner.provider, owner.owner_kind, owner.owner_id))
            return SimpleNamespace(tools=live)

    base = _gateway_base(tmp_path, _owner_pool=Pool())
    management = plugin_command_service._scope_management(base, _scope(), False)
    assert management.context.live_registry is live and peeked == [("feishu", "user", "alice")]
    result = management.command("/plugins list", revision=management.catalog().revision, request_id="")
    assert result["message"].endswith("computer_use  运行中  已发布 18 个工具")


def test_management_context_default_has_no_registry(tmp_path):
    service, _source = manager(tmp_path)
    assert service.context.live_registry is None
    assert replace(service.context, live_registry="x").live_registry == "x"
