"""W3 固定代发送门和迟到发布：真实 CAS，Event 控制交错，内存传输不证明 OS 清理。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.tests.test_cpack_w3_hot_path_bounds import _aged
from agent_py_agent.tests.test_cpack_w3_lazy_activation import component_client, pack_registry


def _revoke(store, entry):
    return store.change_activation(PluginActivationRequest(
        "disable", entry.revision, replace(entry.activation, phase="revoked"))).installation


def test_revoke_between_facade_fence_and_ensure_refuses_before_start(tmp_path, monkeypatch):
    client, store, entry, _, starts, calls = component_client(tmp_path, monkeypatch)
    tool = client.pack_tools[0]
    original = tool._activation_revoked
    def fence():
        revoked = original()
        _revoke(store, entry)
        return revoked
    monkeypatch.setattr(tool, "_activation_revoked", fence)
    try:
        result = tool.execute({})
        assert not result.ok and result.error_code == "TOOL_UNAVAILABLE"
        assert result.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
        assert result.effect_outcome == "not_started"
        assert not starts and not calls and client._in_flight == 0
    finally:
        client.stop()


def test_late_publication_after_revoke_never_resurrects_tools(tmp_path, monkeypatch):
    client, store, entry, _, starts, calls = component_client(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = client.discover_tools
    def discovered(transport):
        tools = original(transport)
        entered.set()
        assert release.wait(5)
        return tools
    monkeypatch.setattr(client, "discover_tools", discovered)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(client.pack_tools[0].execute, {})
            assert entered.wait(3)
            _revoke(store, entry)
            release.set()
            outcome = result.result(timeout=5)
        assert not outcome.ok and outcome.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
        assert outcome.effect_outcome == "not_started" and not calls
        assert not client.validated_tools and client._in_flight == 0 and starts[0].stops >= 1
    finally:
        release.set()
        client.stop()


def test_warm_display_cache_cannot_authorize_send_and_boundary_retires_old_client(tmp_path, monkeypatch):
    client, store, entry, owner, starts, calls = component_client(tmp_path, monkeypatch)
    registry = pack_registry(tmp_path, owner)
    registry._mcp_clients.append(client)
    tool = client.pack_tools[0]
    registry.tools[tool.model_spec.name] = tool
    try:
        assert tool.execute({}).ok
        _aged(owner)
        assert tool.availability().available and tool.precheck_availability().available
        _revoke(store, entry)
        sent = len(calls)
        outcome = tool.execute({})
        assert not outcome.ok and outcome.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
        assert outcome.effect_outcome == "not_started" and len(calls) == sent
        assert starts[0].stops == 0
        registry.prepare_for_run()
        assert client.is_closed() and starts[0].stops >= 1
        assert tool.model_spec.name not in registry.tools
        assert len(starts) == 1
    finally:
        registry.close_mcp_clients()
