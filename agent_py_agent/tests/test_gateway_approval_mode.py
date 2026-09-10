"""权限菜单服务复用现有认证、冷 owner 解析；正文不能冒充管理员。"""

import json
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts import approval_mode_service, http_handlers
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.approval_mode import read_permission_mode
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_model_profiles import Handler
from agent_py_agent.tests.test_owner_approval_mode import owner_home


def test_permissions_api_auth_owner_and_admin_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "tui"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))
    monkeypatch.setattr(approval_mode_service, "resolve_gateway_scope_owner",
                        lambda agent, scope: OwnerIdentity.provider_user("tui", scope.user_id))
    server = SimpleNamespace(agent=SimpleNamespace(home_paths=home_paths(tmp_path), config=AgentConfig()))
    handler = Handler({"operation": "set", "conversation_id": "s", "mode": "auto", "user_id": "bob",
                       "is_admin": True, "owner_provider": "local", "owner_kind": "main"})
    approval_mode_service.handle_client_approval_mode(handler, server)
    assert handler.reply[0] == 200 and not handler.reply[1]["is_admin"]
    assert read_permission_mode(owner_home(tmp_path)) == "auto"
    assert read_permission_mode(owner_home(tmp_path, "bob")) == "ask"
    handler.body["mode"] = "full-access"
    approval_mode_service.handle_client_approval_mode(handler, server)
    assert handler.reply[0] == 400
    assert read_permission_mode(owner_home(tmp_path)) == "auto"
    monkeypatch.setattr(approval_mode_service, "resolve_gateway_scope_owner", lambda agent, scope: OwnerIdentity.local_main())
    approval_mode_service.handle_client_approval_mode(handler, server)
    assert handler.reply[0] == 200 and handler.reply[1]["is_admin"]
    assert read_permission_mode(owner_home(tmp_path, admin=True)) == "full-access"


def test_untrusted_permission_request_never_reads_or_writes(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: True)
    handler = Handler(None)
    approval_mode_service.handle_client_approval_mode(handler, SimpleNamespace(agent=object()))
    assert handler.reply is None


def test_permission_endpoint_does_not_echo_unexpected_errors(monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda handler: (handler.user, "local"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: (handler.user, None))

    def broken(*args):
        raise RuntimeError("private-key-must-not-escape")

    monkeypatch.setattr(approval_mode_service, "resolve_gateway_scope_owner", broken)
    handler = Handler({"operation": "get", "conversation_id": "s"})
    approval_mode_service.handle_client_approval_mode(handler, SimpleNamespace(agent=object()))
    assert handler.reply[0] == 500 and "private-key" not in json.dumps(handler.reply)
