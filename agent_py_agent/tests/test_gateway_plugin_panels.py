from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import http_handlers, plugin_panels_http
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
