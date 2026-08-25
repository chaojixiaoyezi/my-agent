"""审计 #1 修复真测:网关默认绑 loopback + 暴露须鉴权(fail-closed)+ 写端点权限校验。

真起 HTTP server、真发请求(urllib),断言:默认只绑 127.0.0.1;绑非 loopback 无鉴权拒绝启动;
伪造非管理员外部通道身份派工/停网关被 403;本机回环终端仍 admin 放行(单机路径不破)。
学 通道运行时:默认 loopback、暴露到网络须鉴权。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.gateway_parts.bounded_http_server import (
    GatewayBoundedHTTPServer,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
    _is_loopback_host,
)


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root / "gateway"
        self.inbox = self.root / "requests" / "pending"
        self.processing = self.root / "requests" / "processing"
        self.responses = self.root / "responses"
        self.stop_request = self.root / "gateway_stop.request"
        self.state = self.root / "gateway_state.json"
        for p in (self.inbox, self.processing, self.responses):
            p.mkdir(parents=True, exist_ok=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _auth_mw() -> AuthMiddleware:
    return AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))


def _post(port: int, path: str, headers: dict | None = None, body: dict | None = None) -> int:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def test_is_loopback_host_classification() -> None:
    assert _is_loopback_host("127.0.0.1") and _is_loopback_host("localhost") and _is_loopback_host("::1")
    assert not _is_loopback_host("")  # 空 = 通配 = 暴露
    assert not _is_loopback_host("0.0.0.0")
    assert not _is_loopback_host("192.168.1.5")


def test_default_bind_is_loopback(tmp_path) -> None:
    server = GatewayHTTPServer(_free_port(), _Paths(tmp_path))
    server.start()
    try:
        assert server.server.server_address[0] == "127.0.0.1"  # 默认只绑回环,不暴露
    finally:
        server.stop()


def test_gateway_server_absorbs_multi_tui_reconnect_bursts() -> None:
    """单 Gateway 复用固定 worker，并给重连突发保留有界容量。"""
    assert GatewayBoundedHTTPServer.request_queue_size >= 128
    assert GatewayBoundedHTTPServer.max_request_workers == 16
    assert (
        GatewayBoundedHTTPServer.max_outstanding_requests
        >= GatewayBoundedHTTPServer.max_request_workers
    )


def test_refuse_nonloopback_without_auth(tmp_path) -> None:
    server = GatewayHTTPServer(_free_port(), _Paths(tmp_path), params=GatewayHTTPServerParams(bind_host="0.0.0.0"))
    with pytest.raises(RuntimeError, match="拒绝启动|鉴权"):
        server.start()  # fail-closed:暴露到网络却无鉴权 → 拒绝启动
    server.stop()  # 守卫在建 server 前抛,stop 安全无副作用


def test_nonloopback_with_auth_starts(tmp_path) -> None:
    server = GatewayHTTPServer(
        _free_port(), _Paths(tmp_path),
        params=GatewayHTTPServerParams(bind_host="0.0.0.0", auth_middleware=_auth_mw()),
    )
    server.start()  # 有鉴权 → 允许暴露
    try:
        assert server.server.server_address[0] == "0.0.0.0"
    finally:
        server.stop()


def test_trusted_channel_user_can_submit_but_not_admin(tmp_path) -> None:
    # #2 修正:回环本机 = 可信来源,渠道用户(经适配器转发)可提交自己的任务(202),
    # 但不是 admin —— 停网关被 403。远程不可信来源的拒绝在 test_gateway_identity_trust 的中间件级真测。
    port = _free_port()
    server = GatewayHTTPServer(port, _Paths(tmp_path), params=GatewayHTTPServerParams(auth_middleware=_auth_mw()))
    server.start()
    try:
        assert _post(port, "/ask", headers={"X-Channel": "feishu", "X-User-Id": "bob"},
                     body={"kind": "ask", "goal": "hello"}) == 202  # 可信渠道用户可派工
        assert _post(port, "/stop", headers={"X-Channel": "feishu", "X-User-Id": "bob"}) == 403  # 但非管理员
    finally:
        server.stop()


def test_loopback_terminal_still_admin(tmp_path) -> None:
    port = _free_port()
    server = GatewayHTTPServer(port, _Paths(tmp_path), params=GatewayHTTPServerParams(auth_middleware=_auth_mw()))
    server.start()
    try:
        # 无身份头 = 本机终端 = admin → 派工放行(单机路径不破)
        status = _post(port, "/ask", body={"kind": "ask", "goal": "hello"})
        assert status == 202
    finally:
        server.stop()


def test_stop_requires_admin(tmp_path) -> None:
    port = _free_port()
    server = GatewayHTTPServer(port, _Paths(tmp_path), params=GatewayHTTPServerParams(auth_middleware=_auth_mw()))
    server.start()
    try:
        assert _post(port, "/stop", headers={"X-Channel": "feishu", "X-User-Id": "attacker"}) == 403  # 非管理员停不了
        assert _post(port, "/stop") == 200  # 本机终端 admin 可停
    finally:
        server.stop()
