"""实际HTTP两个母入口角色门；模拟冻结拒事实不授新角色，C使用原lease/claim组合。"""
import copy
from http.server import BaseHTTPRequestHandler

import pytest

from agent_py_agent.agent.common import global_write_guard
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case


@pytest.mark.parametrize('entry', ['start', 'ctor'])
@pytest.mark.parametrize('actor', ['worker', 'executor'])
def test_original_http_entry_without_runtime_rejects_restricted_actor(tmp_path, monkeypatch, entry, actor):
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'original-C') if actor == 'worker' else None)
    monkeypatch.setattr(global_write_guard, '_restricted_executor', actor == 'executor')
    server = None
    try:
        with pytest.raises(RuntimeError, match='GLOBAL_WRITE_IN_'):
            if entry == 'ctor':
                server = GatewayBoundedHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
            else:
                from types import SimpleNamespace
                server = GatewayHTTPServer(0, SimpleNamespace(root=tmp_path, state=tmp_path/'state.json'))
                server.start()
    finally:
        if server is not None:
            server.server_close() if entry == 'ctor' else server.stop()


@pytest.mark.parametrize('fault', ['missing', 'copy', 'wrong_control', 'closed'])
def test_current_c_http_start_requires_original_runtime_before_any_side_effect(tmp_path, monkeypatch, fault):
    with curator_case(tmp_path, monkeypatch) as (agent, _service, _backend, gate, _claim, close):
        control = coordinator_for_root(gate.paths.root, context=gate.paths).control_plane
        assert type(control) is GatewayControlPlane
        runtime = GatewayDrainRuntime(control)
        selected = None if fault == 'missing' else copy.copy(runtime) if fault == 'copy' else runtime
        supplied_control = copy.copy(control) if fault == 'wrong_control' else control
        server = GatewayHTTPServer(0, gate.paths, params=GatewayHTTPServerParams(agent=agent, control_plane=supplied_control, drain_runtime=selected))
        calls = []
        monkeypatch.setattr(server, '_prepare_local_credential', lambda: calls.append('credential'))
        if fault == 'closed':
            close()
        try:
            with pytest.raises((RuntimeError, ValueError), match='drain_original|http_original'):
                server.start()
            assert calls == []
            assert server.server is None and server._thread is None
        finally:
            if server.server is not None:
                server.stop()


def test_current_c_http_original_pool_and_listener_birth_then_actual_join(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _service, _backend, gate, _claim, _close):
        control = coordinator_for_root(gate.paths.root, context=gate.paths).control_plane
        assert type(control) is GatewayControlPlane
        runtime = GatewayDrainRuntime(control)
        server = GatewayHTTPServer(0, gate.paths, params=GatewayHTTPServerParams(agent=agent, control_plane=control, drain_runtime=runtime))
        assert agent.home_paths.root.resolve().is_relative_to(tmp_path.resolve())
        try:
            server.start()
            assert server._thread is runtime._resources['http/listener'].owner
            assert server.server._request_executor is runtime._resources['http/requests'].owner
            runtime.require_original()
            runtime.stop_producers()
        finally:
            server.stop()
        import time
        assert all(group.observe(time.monotonic()+2) == 'settled' for group in runtime._resources.values())
