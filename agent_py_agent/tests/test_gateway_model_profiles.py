"""模型配置 API 的 owner、错误脱敏与无聊天副作用合同。"""

import json
from types import SimpleNamespace
from uuid import uuid4

from agent_py_agent.agent.gateway_parts import http_handlers, model_profile_service
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_model_profiles import Host, profile


class Handler:
    _auth_middleware = object()

    def __init__(self, body, user="alice"):
        self.body = body
        self.user = user
        self.reply = None

    def _read_json(self):
        return self.body

    def _send_json(self, status, payload):
        self.reply = (status, payload)


def test_model_api_uses_authenticated_owner_and_does_not_return_secrets(tmp_path, monkeypatch):
    base = Host(tmp_path)
    base.home_paths = home_paths(tmp_path)
    owners = {name: SimpleNamespace(config=base.config, home_paths=home_paths_with_owner(
        base.home_paths, resolve_owner_home(tmp_path, OwnerIdentity.provider_user("local", name)),
    )) for name in ("alice", "bob")}
    scopes = []

    def resolve(base, scope):
        scopes.append(scope)
        return OwnerIdentity.provider_user("local", scope.user_id)

    monkeypatch.setattr(model_profile_service, "resolve_gateway_scope_owner", resolve)
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))
    profile_id = str(uuid4())
    request = Handler({"operation": "add", "conversation_id": "sess-a", "user_id": "bob",
                       "profile_id": profile_id, "profile": profile()})
    model_profile_service.handle_client_models(request, SimpleNamespace(agent=base))
    assert request.reply[0] == 200 and request.reply[1]["ok"]
    assert scopes[-1].user_id == "alice"
    assert "secret" not in json.dumps(request.reply)
    assert len(execute_model_profile_operation(owners["bob"], "list", {})["profiles"]) == 1
    denied = Handler({"operation": "select", "conversation_id": "sess-b", "profile_id": profile_id}, "bob")
    model_profile_service.handle_client_models(denied, SimpleNamespace(agent=base))
    assert denied.reply[0] == 400


def test_model_api_does_not_echo_unexpected_error_or_bad_json(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))

    def broken(*args):
        raise ValueError("private-secret-from-a-different-layer")

    monkeypatch.setattr(model_profile_service, "resolve_gateway_scope_owner", broken)
    handler = Handler({"operation": "list", "conversation_id": "s"})
    model_profile_service.handle_client_models(handler, SimpleNamespace(agent=object()))
    assert handler.reply[0] == 500 and "private-secret" not in json.dumps(handler.reply)


def test_model_api_rejects_untrusted_before_reading_body(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: True)
    handler = Handler(None)
    model_profile_service.handle_client_models(handler, SimpleNamespace(agent=object()))
    assert handler.reply is None


def test_cold_owner_model_menu_does_not_initialize_agent(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_worker

    initialized = []

    def forbidden_pool(agent):
        initialized.append(True)
        raise AssertionError("模型菜单不能创建 owner Agent 或启动后台服务")

    monkeypatch.setattr(request_worker, "_owner_pool", forbidden_pool)
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))
    server = SimpleNamespace(agent=SimpleNamespace(
        home_paths=home_paths(tmp_path),
        config=AgentConfig(gateway_per_user_owner_scoping=True),
    ))
    profile_id = str(uuid4())
    operations = [
        {"operation": "list"},
        {"operation": "add", "profile_id": profile_id, "profile": profile()},
        {"operation": "select", "profile_id": profile_id},
    ]
    for payload in operations:
        handler = Handler({"conversation_id": "cold-session", **payload})
        model_profile_service.handle_client_models(handler, server)
        assert handler.reply[0] == 200 and handler.reply[1]["ok"]
    assert handler.reply[1]["selected"] == profile_id
    assert initialized == []
    assert not (tmp_path / "owners").exists()
