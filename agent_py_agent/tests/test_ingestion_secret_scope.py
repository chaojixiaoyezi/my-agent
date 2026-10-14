"""正式 watch HTTP 来源的宿主秘密边界；仅使用临时 owner 和本机合成监听器。"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


class _Sink(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.received.append((self.path, dict(self.headers)))
        raw = json.dumps({'alive': True, 'echo': self.headers.get('Authorization', '')}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def sink():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Sink)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(2)
    server.server_close()


# LLM: 当前主体与允许本机测试地址由合成宿主构造，秘密不进入模型输入；普通、群和本机身份均使用原Core/Registry。
# 函数用途: 生成测试 owner 和正式 watch 工具，不插入权限旁路或替换 HTTP handler。
def _agent(tmp_path, kind='user', full=False):
    return SimpleAgent(AgentConfig(model_backend='echo', enable_plugins=False, my_agent_home=str(tmp_path / 'home'),
        my_agent_owner_provider='local' if kind == 'main' else 'feishu', my_agent_owner_kind=kind,
        my_agent_owner_id={'main': 'main', 'user': 'ou_synthetic', 'group': 'oc_synthetic'}[kind],
        access_mode='full-access' if full else 'workspace-write'), tmp_path / 'workspace')


# LLM: 每个测试来源不同ID，所有调用仍经原 Registry/Executor、网络安全门和正式secret choke；关闭只回收该临时watch。
# 函数用途: 执行一次来源探针，保留实际 HTTP 接收事实，避免留下持续收割线程。
def _open(agent, sink, ref, *, call_id=None):
    source = call_id or uuid.uuid4().hex
    url = f'http://127.0.0.1:{sink.server_port}/{source}'
    result = execute_registry_test_call(agent.tools, 'watch_stream', {'action': 'open', 'url': url, 'mode': 'poll',
        'source_id': source, 'poll_query_seconds': 120,
        'http_request': {'method': 'GET', 'secret_bindings': [{'location': 'header', 'name': 'Authorization', 'secret_ref': ref}]}},
        write_boundary={'allowed_private_hosts': ['127.0.0.1'], 'allow_private_resolution': True},
        register_with=agent, call_id=uuid.uuid4().hex)
    if result.ok:
        execute_registry_test_call(agent.tools, 'watch_stream', {'action': 'close', 'url': url},
            register_with=agent, call_id=uuid.uuid4().hex)
    return result


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('reference_kind', ['env', 'foreign_file'])
def test_model_source_cannot_export_host_environment_or_owner_outside_file(tmp_path, sink, monkeypatch, kind, full, reference_kind):
    agent = _agent(tmp_path, kind, full)
    marker = 'SYNTHETIC_HOST_MARKER_91e2'
    if reference_kind == 'env':
        monkeypatch.setenv('SYNTHETIC_HOST_CREDENTIAL_91e2', marker)
        ref = 'env:SYNTHETIC_HOST_CREDENTIAL_91e2'
    else:
        outside = tmp_path / 'host-private.txt'
        outside.write_text(marker)
        ref = 'file:' + str(outside)
    result = _open(agent, sink, ref)
    assert not result.ok
    assert not sink.received
    assert marker not in result.output


def test_owner_authorized_file_ref_still_works_and_echo_is_redacted(tmp_path, sink):
    agent = _agent(tmp_path)
    path = agent.home_paths.owner_workspace_dir / 'api-token.txt'
    path.write_text('SYNTHETIC_OWNER_TOKEN_91e2')
    result = _open(agent, sink, 'file:' + str(path))
    assert result.ok, (result.error_code, result.output)
    assert sink.received and all(row[1].get('Authorization') == 'SYNTHETIC_OWNER_TOKEN_91e2' for row in sink.received)
    assert 'SYNTHETIC_OWNER_TOKEN_91e2' not in result.output
