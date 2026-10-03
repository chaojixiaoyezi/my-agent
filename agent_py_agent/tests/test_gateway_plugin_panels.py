from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import http_handlers, plugin_panels_http
from agent_py_agent.agent.plugin_channel import PluginChannelRevoked
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_gateway_plugin_commands import Handler


class _RecordingService:
    def __init__(self):
        self.queries = []

    def panels(self, query):
        self.queries.append(query)
        return [{"plugin_id": plugin_id, "panel_id": panel_id, "state": "loading"}
                for plugin_id, panel_id in query.requested]


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))
    agent = SimpleNamespace(config=AgentConfig(gateway_per_user_owner_scoping=True, enable_plugins=True),
                            home_paths=home_paths(tmp_path))
    return SimpleNamespace(agent=agent, plugin_display=_RecordingService())


def _call(server, body, user="alice"):
    handler = Handler(body, user=user)
    plugin_panels_http.handle_client_plugin_panels(handler, server)
    return handler.reply


def test_source_authentication_precedes_body(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: True)
    handler = Handler(None)
    handler._read_json = Mock(side_effect=AssertionError("未认证不能读正文"))
    plugin_panels_http.handle_client_plugin_panels(handler, None)
    handler._read_json.assert_not_called()


@pytest.mark.parametrize("body", [None, [], {}, {"panels": "x"}, {"panels": [{"plugin_id": 1, "panel_id": "a"}]},
                                  {"panels": [["pet", "line"]]}])
def test_malformed_requests_are_rejected(server, body):
    status, payload = _call(server, body)
    assert status == 400 and "error" in payload
    assert server.plugin_display.queries == []


def test_disabled_plugins_return_no_panels(server):
    server.agent.config = AgentConfig(gateway_per_user_owner_scoping=True, enable_plugins=False)
    status, payload = _call(server, {"conversation_id": "s1", "panels": [{"plugin_id": "pet", "panel_id": "line"}]})
    assert status == 200 and payload == {"ok": True, "panels": []}
    assert server.plugin_display.queries == []


def test_cold_owner_uses_host_scope_and_empty_activity_without_loading_agent(server, tmp_path):
    body = {"conversation_id": "s1", "panels": [{"plugin_id": "pet", "panel_id": "line"}]}
    status, payload = _call(server, body)
    spoofed = dict(body, user_id="bob", owner_id="main", metadata={"user_id": "bob"})
    _call(server, spoofed)
    other = _call(server, body, user="bob")
    assert status == 200 and payload["panels"][0]["state"] == "loading"
    first, spoof, bob = server.plugin_display.queries
    assert first.activity == {} and first.requested == (("pet", "line"),)
    assert first.owner_key == spoof.owner_key != bob.owner_key
    assert first.thread_key == "conversation:s1"
    assert other[0] == 200
    assert not hasattr(server.agent, "_owner_pool")


def test_server_exposes_one_shared_channel_pool_for_panels_and_events():
    """面板和以后的事件中心必须用同一个池；面板服务关闭时不能把共用池关掉。"""
    server = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(plugin_process_sandbox=True)))

    first = plugin_panels_http.plugin_channel_pool(server)
    second = plugin_panels_http.plugin_channel_pool(server)
    assert first is second and first is server.plugin_channel_pool

    service = plugin_panels_http.plugin_display_service(server)
    assert service._pool is first, "面板服务没有用 server 上那一个共用池"
    service.close()
    # 面板服务关闭只清自己的缓存；共用池还归 Gateway 管，事件中心后面还要用它
    assert server.plugin_channel_pool is first
    assert first.acquire("owner", _installation_stub(), 1.0).key == ("owner", "act-1")
    first.close()
    with pytest.raises(PluginChannelRevoked):
        first.acquire("owner", _installation_stub(), 1.0)


def test_self_owned_pool_is_closed_with_the_service(monkeypatch, tmp_path):
    """没注入池时服务自建一个，close 必须把它关掉（沙箱外复核用的默认路径）。"""
    from agent_py_agent.agent.plugin_display.service import DisplayWiring, PluginDisplayService

    service = PluginDisplayService(wiring=DisplayWiring(installations=lambda _owner: (), clock=lambda: 1.0))
    pool = service._pool
    service.close()
    assert pool.connections == {}


# LLM: 只给共用池的用例用：假安装记录只提供池需要的 activation 编号。
def _installation_stub():
    return SimpleNamespace(activation=SimpleNamespace(activation_id="act-1"), enabled=True)


# LLM: 转正 9b 探针 1（停机后重建池）。server_close() 不等还在跑的工作线程，stop 之后晚到的
#   面板请求或事件中心取池会再建一个没人关的打开池，它拉起的插件进程之后再也没人收。
def test_late_panel_request_after_stop_cannot_rebuild_an_open_pool():
    from agent_py_agent.agent.gateway_parts import http_service

    server = http_service.GatewayHTTPServer.__new__(http_service.GatewayHTTPServer)
    server.server = None
    server._thread = None
    server.plugin_display = None
    server.plugin_channel_pool = None
    server.agent = SimpleNamespace(config=SimpleNamespace(plugin_process_sandbox=False))
    first_service = plugin_panels_http.plugin_display_service(server)
    first_pool = server.plugin_channel_pool

    server.stop()

    assert first_pool.closed is True, "stop 没有关掉已有池"
    assert server.plugin_channel_pool is None and server.plugin_display is None
    assert server.plugin_channel_closed is True

    # 晚到的两个入口都必须拒绝，而不是悄悄建一个新的
    with pytest.raises(PluginChannelRevoked):
        plugin_panels_http.plugin_display_service(server)
    with pytest.raises(PluginChannelRevoked):
        plugin_panels_http.plugin_channel_pool(server)
    assert server.plugin_channel_pool is None, "停机后又建出了新池"
    first_service.close()
