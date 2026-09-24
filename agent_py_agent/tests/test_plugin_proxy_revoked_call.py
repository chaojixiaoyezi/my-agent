"""停用后的插件代理调用不管连接状态如何都固定报 TOOL_UNAVAILABLE、未执行；激活仍在时沿原 MCP 执行链。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.plugin_runtime import PluginProxyTool
from agent_py_agent.agent.tooling.mcp_client import MCPError


# 类用途: 假插件客户端；激活是否有效和连接是否已关分别可控，记录是否真的发出了业务调用。
class _Client:
    def __init__(self, *, revoked: bool):
        self.calls = 0
        self.activation_ref = SimpleNamespace(require=self._require)
        self._revoked = revoked

    def _require(self, *, allow_preparing: bool = False):
        if self._revoked:
            raise ValueError("插件激活已撤销")
        return SimpleNamespace(enabled=True)

    def call_tool(self, tool_name, arguments, **options):
        self.calls += 1
        raise MCPError("MCP 连接已关闭或被替换", code="MCP_CONNECTION_CLOSED", effect_outcome="not_started")


def _tool(client) -> PluginProxyTool:
    return PluginProxyTool(client, "read", SimpleNamespace(name="plugin__sample_peek_07b51450__read_3316348d"), SimpleNamespace())


def test_revoked_activation_wins_over_closed_connection_and_never_sends():
    client = _Client(revoked=True)
    outcome = _tool(client).execute({"path": "/tmp/x"})
    assert outcome.ok is False and outcome.error_code == "TOOL_UNAVAILABLE"
    assert outcome.effect_outcome == "not_started"
    assert outcome.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE", "具体原因码与审批前/批准后复核一致"
    assert json.loads(outcome.output)["error"] == "原插件已停用或激活不可用"
    assert client.calls == 0, "撤销后不再向连接发送，也不看连接是否已关"


def test_live_activation_falls_through_to_the_mcp_call_path():
    client = _Client(revoked=False)
    outcome = _tool(client).execute({"path": "/tmp/x"})
    assert client.calls == 1
    assert outcome.error_code == "TOOL_EXECUTION_FAILED", "连接确实已关时仍是原 MCP_CONNECTION_CLOSED 映射"
