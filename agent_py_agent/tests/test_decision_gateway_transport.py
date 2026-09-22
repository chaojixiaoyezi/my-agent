"""原菜单运输到本机 Gateway 设置服务再到本机决策 HTTP；不启动共享 Gateway 或真实模型。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.gateway_parts import http_handlers, model_profile_service
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
from agent_py_agent.cli.chat_parts.tui_model_menu import _request_data_sync
from agent_py_agent.tests import test_decision_service_http as native_fixture
from agent_py_agent.tests.test_decision_service_http import server as server
from agent_py_agent.tests.test_model_profiles import profile


# LLM: 本机测试容器只替代认证中间件，固定按原客户端身份头建立可信测试身份；服务及双HTTP传输仍用产品实现。
# 函数用途: 启动随机端口的模型管理路由，退出回收服务线程，不使用日常配置或公共 Gateway。
@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda _handler: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda h: (h.headers["X-User-Id"], h.headers["X-Channel"]))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda h: (h.headers["X-User-Id"], None))
    app = SimpleNamespace(agent=SimpleNamespace(home_paths=home_paths(tmp_path), config=AgentConfig(gateway_per_user_owner_scoping=True)))
    seen = []

    # LLM: 仅实现生产管理服务需要的容器接口，业务处理全部进入 handle_client_models；错误不伪造成功。
    # 类用途: 接受真实薄客户端 POST 字节并返回原服务回执。
    class Handler(BaseHTTPRequestHandler):
        _auth_middleware = object()

        # LLM: 仅分派已知测试路由，不运行 ask、任意命令或任务队列。
        # 函数用途: 把真实请求交给原模型管理服务。
        def do_POST(self):
            assert self.path == "/client/models"
            model_profile_service.handle_client_models(self, app)

        # LLM: 保留收到的原字节投影供身份覆盖断言；不修改后端payload。
        # 函数用途: 为原服务读取测试请求正文。
        def _read_json(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            return body

        # LLM: 完整返回原服务状态和JSON，不吞400/500；不创建其它副作用。
        # 函数用途: 将原服务回执编码成HTTP响应。
        def _send_json(self, status, payload):
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        # LLM: 测试不打印身份或请求载荷；失败证据由断言处理。
        # 函数用途: 关闭默认HTTP日志。
        def log_message(self, *_args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: http.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    client = GatewayChatClientAgent(None, AgentConfig(gateway_port=http.server_port), tmp_path, [tmp_path], home_paths(tmp_path),
                                   owner_identity=OwnerIdentity.provider_user("local", "alice"))
    try:
        yield client, seen
    finally:
        http.shutdown()
        http.server_close()
        worker.join(2)


def test_original_menu_thin_http_gateway_and_native_probe_share_settings(gateway, server, monkeypatch):
    client, seen = gateway
    monkeypatch.setattr(native_fixture, "response", lambda: {"model": "jev-test", "answers": {"connection": {
        "type": "choice", "choice": "ready", "confidence": 1.0, "probabilities": {"ready": 1.0, "unknown": 0.0}}},
        "usage": {"input_tokens": 23}})
    key = str(uuid4())
    added = _request_data_sync(client, "menu", "add", {"profile_id": key, "profile": profile(api_base=server.url,
        model_name="jev-test", model_backend="typesafe_decision", capability="decision")})
    assert added["ok"] and added["selected"] == "default"
    read = _request_data_sync(client, "menu", "decision_read", {"decision": {"scope": "thread"}})
    assert read["ok"] and not read["effective"]["enabled"]
    changed = _request_data_sync(client, "menu", "decision_patch", {"user_id": "bob", "decision": {
        "scope": "thread", "expected_revision": read["revision"], "changes": {"profile_id": key, "timeout_seconds": 6.5}}})
    assert changed["ok"] and changed["effective"]["timeout_seconds"] == 6.5
    assert seen[-1]["user_id"] == "alice"
    assert not server.requests  # 配置读取/保存不发模型请求。
    probe = _request_data_sync(client, "menu", "decision_probe", {"profile_id": key, "timeout_seconds": 1})
    assert probe["ok"] and probe["usage"]["input_tokens"] == 23
    assert len(server.requests) == 1
    after = _request_data_sync(client, "menu", "decision_read", {"decision": {"scope": "thread"}})
    assert after["revision"] == changed["revision"] and not after["effective"]["enabled"]
    listed = _request_data_sync(client, "menu", "list", {})
    assert listed["selected"] == added["selected"]
    client.owner_identity = OwnerIdentity.provider_user("local", "bob")
    denied = _request_data_sync(client, "menu", "decision_probe", {"profile_id": key})
    assert not denied["ok"] and len(server.requests) == 1


@pytest.mark.parametrize("status", [402, 503])
def test_gateway_probe_failure_does_not_prevent_disabling(gateway, server, status):
    client, _seen = gateway
    key = str(uuid4())
    assert _request_data_sync(client, "errors", "add", {"profile_id": key, "profile": profile(api_base=server.url,
        model_name="jev-test", model_backend="typesafe_decision", capability="decision")})["ok"]
    view = _request_data_sync(client, "errors", "decision_read", {})
    view = _request_data_sync(client, "errors", "decision_patch", {"decision": {"expected_revision": view["revision"],
        "changes": {"enabled": True, "profile_id": key}}})
    server.status = status
    failure = _request_data_sync(client, "errors", "decision_probe", {"profile_id": key})
    assert not failure["ok"] and failure["error_type"] and len(server.requests) == 1
    saved = _request_data_sync(client, "errors", "decision_patch", {"decision": {"expected_revision": view["revision"],
        "changes": {"enabled": False}}})
    assert saved["ok"] and not saved["effective"]["enabled"]
    assert len(server.requests) == 1
