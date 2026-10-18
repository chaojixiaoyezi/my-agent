"""C HTTP真实参数与设施入口最早拒门，原红哨兵禁止创建pool/socket。"""
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_py_agent.agent.common import global_write_guard
from agent_py_agent.agent.gateway_parts import bounded_http_server, http_service
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition


@pytest.mark.parametrize('name', ['control_plane', 'drain_runtime'])
def test_http_params_transport_existing_original_C_dependencies(name):
    actual = {field.name: field for field in fields(http_service.GatewayHTTPServerParams)}
    assert name in actual and actual[name].default is None


@pytest.mark.parametrize('actor', ['worker', 'executor'])
def test_restricted_lower_http_constructor_rejects_before_pool_or_socket(monkeypatch, actor):
    monkeypatch.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'http-C') if actor == 'worker' else None)
    monkeypatch.setattr(global_write_guard, '_restricted_executor', actor == 'executor')

    def forbidden(*_args, **_kwargs):
        raise RuntimeError('unexpected_pool_or_socket_birth')

    monkeypatch.setattr(bounded_http_server, 'DurableDaemonThreadPoolExecutor', forbidden)
    monkeypatch.setattr(HTTPServer, '__init__', forbidden)
    with pytest.raises(RuntimeError, match='GLOBAL_WRITE_IN_'):
        bounded_http_server.GatewayBoundedHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler, bind_and_activate=False)
