"""内置 Skill 真实发现、只读探测与认证头不回显；只访问本地 HTTP fixture。"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


# LLM: 路径来自当前测试所在工作树，不使用用户目录或外部安装副本。
# 函数用途: 读取本切片唯一随包探测脚本路径。
def _probe_path():
    return Path(__file__).resolve().parents[1] / 'skills/builtin/plugins/api-onboarding/scripts/endpoint_probe.py'


# LLM: 导入脚本不运行命令入口，后续测试执行真实 probe。
# 函数用途: 加载当前版本探测模块。
def _module():
    spec = importlib.util.spec_from_file_location('fixture_endpoint_probe', _probe_path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# LLM: 只读合成服务记录真实 HTTP 方法与头，绝不连接外部接口。
# 函数用途: 提供自动释放端口的 HTTP fixture。
@pytest.fixture
def endpoint():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            calls.append((self.command, self.headers.get('Authorization')))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
        def do_GET(self):
            self.do_HEAD()
            self.wfile.write(b'{}')
        def log_message(self, format, arg1=None, arg2=None, arg3=None):
            return None
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/health', calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


# LLM: 拒绝写方法须发生在任何网络 I/O 之前。
# 函数用途: 验证 POST 被结构化拒绝且未到达服务端。
def test_probe_refuses_post_without_network(endpoint):
    url, calls = endpoint
    result = _module().probe(url, method='POST')
    assert isinstance(result, dict)
    assert result['error_code'] == 'METHOD_REFUSED' and result['http_status'] is None
    assert calls == []


# LLM: 认证值仅来自测试进程环境，输出合同只允许六个公开事实。
# 函数用途: 验证真实 GET、HEAD、JSON CLI 与认证头不回显。
@pytest.mark.parametrize('method', ['HEAD', 'GET'])
def test_probe_cli_returns_json_and_never_prints_auth_header(endpoint, method):
    url, calls = endpoint
    secret = 'fixture-private-header-value'
    result = subprocess.run([sys.executable, str(_probe_path()), url, '--method', method,
        '--auth-header-env', 'FIXTURE_PROBE_HEADER'], capture_output=True, text=True,
        env={**os.environ, 'FIXTURE_PROBE_HEADER': secret}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('{')
    payload = json.loads(result.stdout)
    assert set(payload) == {'url', 'method', 'http_status', 'latency_ms', 'content_type', 'error_code'}
    assert payload['http_status'] == 200 and payload['method'] == method
    assert payload['content_type'] == 'application/json' and payload['latency_ms'] >= 0
    assert calls == [(method, secret)]
    assert secret not in result.stdout + result.stderr


# LLM: 使用真实内置 SkillsService 与有效 Owner 策略，不能只断言文件存在。
# 函数用途: 验证新 Skill 可解析、可发现且稳定引用读回内容一致。
def test_builtin_skill_loads_through_the_existing_service(tmp_path):
    from agent_py_agent.agent.capability.skill_service import SkillsService
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
    home = replace(home_paths(tmp_path / 'home'), shared_builtin_dir=_probe_path().parents[3])
    service = SkillsService(home_paths=home, workspace_root=tmp_path,
        policy_provider=lambda: resolve_effective_owner_policy(home))
    snapshot = service.snapshot_for(force_reload=True)
    entry = snapshot.resolve('builtin:api-onboarding')
    assert entry is not None, snapshot.errors
    assert entry.source == 'builtin'
    text = Path(entry.path).read_text()
    assert 'manifest_register' in text and 'source_report' in text
    assert 'credential_ref' in text and 'confirmed=true' in text
