"""模型配置 API 的 owner、错误脱敏与无聊天副作用合同。"""

import json
from types import SimpleNamespace
from uuid import uuid4

from agent_py_agent.agent.gateway_parts import http_handlers, model_profile_service
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
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
    owners = {name: Host(tmp_path, name) for name in ("alice", "bob")}
    scopes = []

    def resolve(base, scope):
        scopes.append(scope)
        return owners[scope.user_id]

    monkeypatch.setattr(model_profile_service, "resolve_gateway_scope_agent", resolve)
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))
    profile_id = str(uuid4())
    request = Handler({"operation": "add", "conversation_id": "sess-a", "user_id": "bob",
                       "profile_id": profile_id, "profile": profile()})
    model_profile_service.handle_client_models(request, SimpleNamespace(agent=object()))
    assert request.reply[0] == 200 and request.reply[1]["ok"]
    assert scopes[-1].user_id == "alice"
    assert "secret" not in json.dumps(request.reply)
    assert len(execute_model_profile_operation(owners["bob"], "list", {})["profiles"]) == 1
    denied = Handler({"operation": "select", "conversation_id": "sess-b", "profile_id": profile_id}, "bob")
    model_profile_service.handle_client_models(denied, SimpleNamespace(agent=object()))
    assert denied.reply[0] == 400


def test_model_api_does_not_echo_unexpected_error_or_bad_json(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))

    def broken(*args):
        raise ValueError("private-secret-from-a-different-layer")

    monkeypatch.setattr(model_profile_service, "resolve_gateway_scope_agent", broken)
    handler = Handler({"operation": "list", "conversation_id": "s"})
    model_profile_service.handle_client_models(handler, SimpleNamespace(agent=object()))
    assert handler.reply[0] == 500 and "private-secret" not in json.dumps(handler.reply)


def test_model_api_rejects_untrusted_before_reading_body(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: True)
    handler = Handler(None)
    model_profile_service.handle_client_models(handler, SimpleNamespace(agent=object()))
    assert handler.reply is None
