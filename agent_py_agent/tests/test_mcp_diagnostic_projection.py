"""W9: JSON-RPC diagnostics remain bounded; data never grants replay authority."""
import json

import pytest

from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
from agent_py_agent.agent.tooling.mcp_protocol import MCPError, unwrap_jsonrpc
from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool


def remote_error(data):
    with pytest.raises(MCPError) as caught:
        unwrap_jsonrpc("fixture", {"error": {"code": -32602, "message": "invalid request", "data": data}}, "tools/call")
    return caught.value


@pytest.mark.parametrize("reason,field", [("absent", "name"), ("wrong type", "count")])
def test_rpc_error_retains_reason_field_and_code(reason, field):
    error = remote_error({"reason": reason, "field": field})
    assert error.code == "MCP_REMOTE_ERROR"
    assert getattr(error, "remote_code", None) == -32602
    assert getattr(error, "data", None) == {"reason": reason, "field": field}
    assert error.effect_outcome == ""


def test_rpc_error_drops_secrets_deep_objects_and_limits_unicode():
    data = {"reason": "Bearer bearer-private password=password-private " + "界" * 20000,
            "field": "input", "token": "token-private", "password": "password-private",
            "nested": {"deep": {"token": "deep-private"}}, "object": object()}
    error = remote_error(data)
    safe = getattr(error, "data", None)
    assert safe, "safe reason/field must remain available"
    text = json.dumps(safe, ensure_ascii=False)
    assert len(text.encode("utf-8")) <= 4096
    assert "input" in text
    for secret in ("bearer-private", "password-private", "token-private", "deep-private", "object at"):
        assert secret not in text


class FailingClient:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def call_tool(self, name, arguments, **options):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize("effect", ["", "unknown", "not_started"])
def test_proxy_projects_safe_rpc_diagnostic_without_changing_transport_effect(effect):
    error = remote_error({"reason": "missing", "field": "name", "request_id": "rpc-one"})
    error.effect_outcome = effect
    client = FailingClient(error)
    proxy = build_proxy_tool(client, "fixture", MCPToolInfo("inspect", "fixture", {}))
    outcome = proxy.execute({})
    body = json.loads(outcome.output)
    assert outcome.error_code == "TOOL_EXECUTION_FAILED"
    assert outcome.effect_outcome == effect
    assert client.calls == 1
    assert body.get("mcp_error", {}).get("data", {}).get("reason") == "missing"
    assert body.get("diagnostic", {}).get("code") == "MCP_REMOTE_ERROR"
    assert body.get("diagnostic", {}).get("mcp_error", {}).get("code") == -32602
    assert outcome.result_envelope.get("diagnostic") == body.get("diagnostic")
