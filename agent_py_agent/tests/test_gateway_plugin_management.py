"""Gateway 管理分路合同：真实原执行器和临时 owner，HTTP handler 不进入模型队列。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.gateway_parts.plugin_command_service import handle_client_plugins
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_plugin_package import _bundle


class Handler:
    def __init__(self, body, middleware, *, peer="127.0.0.1", user="local-agent", channel="chat"):
        self.body, self._auth_middleware = body, middleware
        self.headers = {"X-User-Id": user, "X-Channel": channel}
        self.client_address = (peer, 12345) if peer else None
        self.reply = None

    def _read_json(self):
        return self.body

    def _send_json(self, status, payload):
        self.reply = status, payload


def host(tmp_path, *, auth=True, bind="127.0.0.1"):
    middleware = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True)) if auth else None
    base = SimpleNamespace(config=AgentConfig(auth_enabled=auth, gateway_per_user_owner_scoping=True),
                           home_paths=home_paths(tmp_path))
    return SimpleNamespace(agent=base, auth_middleware=middleware, bind_host=bind)


def request(server, body, **identity):
    handler = Handler({"conversation_id": "session-a", **body}, server.auth_middleware, **identity)
    handle_client_plugins(handler, server)
    return handler.reply


def test_admin_install_query_and_disable_use_same_original_request(tmp_path):
    server = host(tmp_path)
    owner = resolve_owner_home(tmp_path)
    owner.home_dir.mkdir(parents=True)
    source = owner.home_dir / "sample.zip"
    source.write_bytes(_bundle())
    status, catalog = request(server, {"operation": "catalog"})
    assert status == 200
    body = {"operation": "command", "command": f'/plugins install "{source}"',
            "catalog_revision": catalog["catalog"]["revision"], "plugin_request_id": "original-a"}
    status, first = request(server, body)
    assert status == 200 and first["state"] == "succeeded", first
    source.unlink()
    assert request(server, body)[1]["state"] == "succeeded"
    server.agent.config = replace(server.agent.config, enable_plugins=False)
    query = request(server, {"operation": "command", "command": "/plugins status original-a"})[1]
    assert query["state"] == "succeeded" and query["details"] == first["details"]
    assert not hasattr(server.agent, "tools") and not hasattr(server.agent, "_owner_pool")


@pytest.mark.parametrize("auth,bind,peer", [(True, "127.0.0.1", "127.0.0.1"),
                                         (False, "0.0.0.0", "127.0.0.1"),
                                         (False, "127.0.0.1", "203.0.113.10"),
                                         (False, "127.0.0.1", None)])
def test_missing_middleware_never_implies_admin(tmp_path, auth, bind, peer):
    server = host(tmp_path, auth=auth, bind=bind)
    server.auth_middleware = None
    result = request(server, {"operation": "command", "command": "/plugins install missing.zip",
                              "plugin_request_id": "a", "catalog_revision": "old"}, peer=peer)[1]
    assert result["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert list(tmp_path.iterdir()) == []


def test_explicit_auth_disabled_loopback_keeps_fixed_local_identity(tmp_path):
    server = host(tmp_path, auth=False)
    first = request(server, {"operation": "catalog"})[1]
    spoof = request(server, {"operation": "catalog", "user_id": "someone", "channel": "external",
                             "owner_id": "someone"})[1]
    assert first == spoof
    assert next(a for a in first["catalog"]["management_actions"] if a["name"] == "install")["available"]
    assert list(tmp_path.iterdir()) == []


def test_trusted_normal_user_cannot_install_or_query_admin_request(tmp_path):
    server = host(tmp_path)
    for command in ["/plugins install missing.zip", "/plugins status request-a"]:
        result = request(server, {"operation": "command", "command": command,
                                  "plugin_request_id": "new-a"}, user="alice", channel="local")[1]
        assert result["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert list(tmp_path.iterdir()) == []
