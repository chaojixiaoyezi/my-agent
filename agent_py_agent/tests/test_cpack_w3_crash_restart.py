"""W3 单激活退避/目录漂移/原错误透传；内存传输用例不替代真实 stdio。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_runtime
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPToolInfo
from agent_py_agent.tests.test_cpack_w3_lazy_activation import component_client, pack_registry


def test_initialize_failure_one_attempt_and_local_backoff(tmp_path, monkeypatch):
    client, _, _, _, starts, _ = component_client(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(plugin_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = client.start
    attempts = []
    def failed():
        attempts.append(1)
        raise MCPError("fixture initialize failure", code="MCP_TIMEOUT")
    monkeypatch.setattr(client, "start", failed)
    tool = client.pack_tools[0]
    try:
        first = tool.execute({})
        assert not first.ok and first.reported_error_code == "MCP_PACK_SERVER_UNAVAILABLE"
        assert first.effect_outcome == "not_started" and attempts == [1]
        assert client.retry_at == 101.0 and client.attempts == 1
        assert tool.precheck_availability().error_code == "MCP_PACK_SERVER_UNAVAILABLE"
        assert not tool.execute({}).ok and attempts == [1]
        clock[0] = 101.0
        monkeypatch.setattr(client, "start", original)
        assert tool.execute({}).ok and len(starts) == 1
        assert client.attempts == 0 and client.retry_at == 0 and client._in_flight == 0
    finally:
        client.stop()


def test_crash_waits_backoff_and_prepare_never_reconnects(tmp_path, monkeypatch):
    client, _, _, _, starts, _ = component_client(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(plugin_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    registry = pack_registry(tmp_path, None)
    registry._mcp_clients.append(client)
    try:
        tool = client.pack_tools[0]
        assert tool.execute({}).ok
        starts[0].revoke()
        registry.prepare_for_run()
        assert len(starts) == 1
        result = tool.execute({})
        assert result.reported_error_code == "MCP_PACK_SERVER_UNAVAILABLE"
        assert result.effect_outcome == "not_started" and len(starts) == 1
        clock[0] = client.retry_at
        assert tool.execute({}).ok and len(starts) == 2
        assert client.pack_tools[0].transport is None
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize("pinned", [False, True])
def test_catalog_drift_is_permanent_and_process_is_retired(tmp_path, monkeypatch, pinned):
    client, _, _, _, starts, calls = component_client(tmp_path, monkeypatch, pinned=pinned)
    monkeypatch.setattr(client, "list_tools", lambda **kwargs: [MCPToolInfo("different", "不同工具", {})])
    try:
        result = client.pack_tools[0].execute({})
        assert not result.ok and result.reported_error_code == "MCP_PACK_CATALOG_DRIFT"
        assert result.effect_outcome == "not_started" and not calls
        assert client.drift and client.is_closed() and starts[0].stops >= 1
        assert not client.pack_tools[0].execute({}).ok and len(starts) == 1
        assert not client.pack_tools[0].availability().available
    finally:
        client.stop()


def test_pinned_comparison_uses_same_normalizer_not_raw_description(tmp_path, monkeypatch):
    client, _, _, _, _, _ = component_client(tmp_path, monkeypatch, pinned=True)
    original = client.list_tools
    monkeypatch.setattr(client, "list_tools", lambda **kwargs: [
        MCPToolInfo(tool.name, "  " + tool.description + "\n", tool.input_schema) for tool in original(**kwargs)])
    try:
        assert client.pack_tools[0].execute({}).ok and not client.drift
    finally:
        client.stop()


def test_business_error_and_sent_unknown_are_not_relabelled_not_started(tmp_path, monkeypatch):
    client, _, _, _, _, _ = component_client(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(client, "call_tool", lambda *args, **kwargs: {"content": "business rejected", "isError": True})
        failed = client.pack_tools[0].execute({})
        assert not failed.ok and failed.error_code == "TOOL_EXECUTION_FAILED" and failed.effect_outcome == "failed"
        def unknown(*args, **kwargs):
            raise MCPError("sent timeout", code="MCP_TIMEOUT", effect_outcome="unknown")
        monkeypatch.setattr(client, "call_tool", unknown)
        uncertain = client.pack_tools[0].execute({})
        assert not uncertain.ok and uncertain.error_code == "TOOL_TIMEOUT" and uncertain.effect_outcome == "unknown"
        assert client._in_flight == 0
    finally:
        client.stop()


def test_unknown_cleanup_never_starts_replacement(tmp_path, monkeypatch):
    client, _, _, _, starts, _ = component_client(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(plugin_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    try:
        assert client.pack_tools[0].execute({}).ok
        starts[0].cleanup_confirmed = False
        starts[0].revoke()
        assert not client.pack_tools[0].execute({}).ok
        clock[0] = client.retry_at
        assert not client.pack_tools[0].execute({}).ok
        assert len(starts) == 1 and client._in_flight == 0
    finally:
        client.stop()
