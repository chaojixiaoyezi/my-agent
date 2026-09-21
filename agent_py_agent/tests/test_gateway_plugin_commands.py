from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.gateway_parts import http_handlers, plugin_command_service, request_worker
from agent_py_agent.agent.plugin_command_catalog import PluginCommandCatalog
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_gateway_model_profiles import Handler as ModelHandler


class Handler(ModelHandler):
    def __init__(self, body, user="alice"):
        super().__init__(body, user)
        self.headers = {"X-User-Id": user, "X-Channel": "local"}
        self.client_address = ("127.0.0.1", 12345)
        self._auth_middleware = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))



@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))
    monkeypatch.setattr(
        request_worker, "_owner_pool", Mock(side_effect=AssertionError("禁止初始化完整 Agent"))
    )
    return SimpleNamespace(
        agent=SimpleNamespace(config=AgentConfig(gateway_per_user_owner_scoping=True), home_paths=home_paths(tmp_path))
    )


def catalog(host, user="alice", session="session-a", **extra):
    handler = Handler({"operation": "catalog", "conversation_id": session, **extra}, user=user)
    plugin_command_service.handle_client_plugins(handler, host)
    assert handler.reply[0] == 200 and handler.reply[1]["ok"]
    return PluginCommandCatalog.from_payload(handler.reply[1]["catalog"])


def test_cold_owner_directory_uses_host_identity_without_loading_agent_or_state(
    host, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    first = catalog(host)
    spoof = catalog(
        host,
        user_id="bob",
        channel="chat",
        owner_id="main",
        scope_ref="spoof",
        metadata={"user_id": "bob", "channel": "chat"},
    )
    assert first == spoof
    assert first.revision != catalog(host, "bob").revision
    assert first.revision != catalog(host, session="session-b").revision
    assert list(tmp_path.iterdir()) == []
    assert not hasattr(host.agent, "_owner_pool")
    assert "alice" not in json.dumps(first.to_payload())
    assert first.plugins == ()


@pytest.mark.parametrize("entry", ["catalog", "ask", "control"])
def test_all_http_command_entries_keep_invalid_and_stale_input_out_of_task_queue(
    host, monkeypatch, entry
):
    monkeypatch.setattr(
        http_handlers,
        "_handle_persistent_control_operation",
        Mock(side_effect=AssertionError("禁止旧控制分派")),
    )
    snapshot = catalog(host)
    texts = [
        ("/plugins help install", "", True, None),
        ("/plugins list --bad", "", False, "INVALID_COMMAND_ARGUMENTS"),
        ("/plugins@missing", "", False, "UNKNOWN_PLUGIN"),
        ("/plugins install sample.whl", "", False, "PLUGIN_PERMISSION_DENIED"),
        ("/plugins install sample.whl", snapshot.revision, False, "PLUGIN_PERMISSION_DENIED"),
        ("/plugins help", "old-revision", False, "PLUGIN_CATALOG_STALE"),
    ]
    for text, revision, ok, error in texts:
        handler = Handler(
            {
                "conversation_id": "session-a",
                "operation": "command",
                "command": text,
                "goal": text,
                "catalog_revision": revision,
            }
        )
        if entry == "catalog":
            plugin_command_service.handle_client_plugins(handler, host)
        elif entry == "ask":
            http_handlers.handle_ask(
                handler, host, Mock(side_effect=AssertionError("禁止创建请求"))
            )
        else:
            http_handlers.handle_control(handler, host)
        assert handler.reply[0] == 200
        result = handler.reply[1]
        assert result["ok"] is ok and result.get("error_code") == error
        assert result["request_id"] == ""
        assert result["catalog"] == snapshot.to_payload()


def test_cross_owner_revision_is_rejected_and_cannot_authorize_another_owner(host):
    alice = catalog(host)
    handler = Handler(
        {
            "operation": "command",
            "command": "/plugins help",
            "conversation_id": "session-a",
            "catalog_revision": alice.revision,
            "owner": "alice",
        },
        user="bob",
    )
    plugin_command_service.handle_client_plugins(handler, host)
    result = handler.reply[1]
    assert result["error_code"] == "PLUGIN_CATALOG_STALE"
    assert result["catalog"] == catalog(host, "bob").to_payload()


def test_declarations_changed_between_discovery_and_submit_are_not_reinterpreted(host, monkeypatch):
    snapshot = catalog(host)
    changed = replace(snapshot, management_actions=())
    monkeypatch.setattr(
        plugin_command_service.PluginManagement, "catalog", lambda *args, **kwargs: changed
    )
    handler = Handler(
        {
            "operation": "command",
            "command": "/plugins help",
            "conversation_id": "session-a",
            "catalog_revision": snapshot.revision,
        }
    )
    plugin_command_service.handle_client_plugins(handler, host)
    assert handler.reply[1]["error_code"] == "PLUGIN_CATALOG_STALE"
    assert handler.reply[1]["catalog"] == changed.to_payload()


def test_source_authentication_precedes_body_and_catalog_access(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: True)
    handler = Handler(None)
    handler._read_json = Mock(side_effect=AssertionError("未认证不能读正文"))
    plugin_command_service.handle_client_plugins(handler, None)
    handler._read_json.assert_not_called()
    assert handler.reply is None


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"operation": "run"},
        {"operation": []},
        {"operation": "command", "command": "/stop"},
        {"operation": "command", "command": ["/plugins"]},
    ],
)
def test_bad_request_does_not_become_control(host, body):
    handler = Handler(body)
    plugin_command_service.handle_client_plugins(handler, host)
    assert handler.reply[0] == 400


def test_directory_errors_are_safe_and_do_not_fallback(host, monkeypatch):
    monkeypatch.setattr(
        plugin_command_service,
        "resolve_gateway_scope_owner",
        Mock(side_effect=RuntimeError("private-secret-path")),
    )
    handler = Handler({"operation": "catalog"})
    plugin_command_service.handle_client_plugins(handler, host)
    assert handler.reply[0] == 503
    assert handler.reply[1]["error_code"] == "PLUGIN_CATALOG_UNAVAILABLE"
    assert "private-secret" not in json.dumps(handler.reply)


def test_real_http_route_roundtrips_catalog_and_preserves_original_identity(tmp_path, monkeypatch):
    import urllib.request

    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.auth.middleware import AuthMiddleware
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.tests.test_gateway_conversation_control import _free_port

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=True), tmp_path
    )
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        request_worker, "_owner_pool", Mock(side_effect=AssertionError("目录不能加载冷用户"))
    )
    server = GatewayHTTPServer(
        _free_port(),
        paths,
        params=GatewayHTTPServerParams(
            agent=agent,
            auth_middleware=AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True)),
        ),
    )

    def post(body, user="alice"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/client/plugins",
            data=json.dumps({"conversation_id": "session-a", **body}).encode(),
            headers={"Content-Type": "application/json", "X-User-Id": user, "X-Channel": "local"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
            return json.load(response)

    server.start()
    try:
        first = post({"operation": "catalog"})
        snapshot = PluginCommandCatalog.from_payload(first["catalog"])
        assert (
            post({"operation": "catalog", "user_id": "bob", "channel": "chat", "owner": "main"})
            == first
        )
        bob = post({"operation": "catalog"}, "bob")
        assert bob["catalog"]["revision"] != snapshot.revision
        result = post(
            {
                "operation": "command",
                "command": "/plugins help install",
                "catalog_revision": snapshot.revision,
            }
        )
        assert result["ok"] and "用法：/plugins install" in result["message"]
        stale = post(
            {
                "operation": "command",
                "command": "/plugins list",
                "catalog_revision": snapshot.revision,
            },
            "bob",
        )
        assert stale["error_code"] == "PLUGIN_CATALOG_STALE"
        assert list(paths.inbox.glob("*.json")) == []
        assert not hasattr(agent, "_owner_pool")
    finally:
        server.stop()
